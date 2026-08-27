"""ADR-B10 自由裁量审查层：解析/合并升级/静默降级/工具层集成。"""
from __future__ import annotations

import json

import pytest
from types import SimpleNamespace
from sqlalchemy.orm import Session

from app.services.ai_reviewer import (AI_CODE, augment, parse_points)
from app.services.llm_client import LLMUnavailable
from app.tools.bootstrap import seed_rules

RAW = ("技术服务合同样本段落：甲方有权随时解除本合同且无需承担责任，乙方不得提出异议，"
       "由此产生的全部损失由乙方自行承担；双方保密义务不设期限，合同终止后依然有效。")


class TestParsePoints:
    def test_valid_payload_cleaned(self) -> None:
        payload = {"points": [
            {"severity": "HIGH", "point": "单方解约权", "evidence_quote": "甲方有权随时解除",
             "reason": "权利不对等"},
            {"severity": "weird", "point": "期限陷阱", "evidence_quote": "无限期"},
            {"severity": "low", "point": "", "evidence_quote": "x"},   # 无 point → 丢弃
            {"severity": "low", "point": "孤儿", "evidence_quote": ""},  # 无证据 → 丢弃
        ]}
        points = parse_points(json.dumps(payload, ensure_ascii=False))
        assert len(points) == 2
        assert points[0]["severity"] == "high"       # 大小写归一
        assert points[1]["severity"] == "low"        # 非法枚举回落 low
        assert points[0]["point"] == "单方解约权"

    def test_no_json_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_points("模型闲聊没有JSON")

    def test_wrapped_in_prose_still_found(self) -> None:
        content = '结论如下：{"points":[{"severity":"medium","point":"p","evidence_quote":"q"}]} 以上'
        assert len(parse_points(content)) == 1


def _base_summary() -> dict:
    return {"overall_risk_level": "low", "overall_risk_label": "低",
            "hits": [{"rule_code": "AUTO_RENEW", "rule_name": "自动续约条款",
                      "risk_level": "medium", "evidence_text": "evi",
                      "evidence_position": "12", "suggestion_text": "注意续约"}],
            "focus_points": ["注意续约"], "stats": {}}


class TestAugment:
    def test_merge_upgrades_overall_and_tags_source(self) -> None:
        from app.services import ai_reviewer as ar

        high = {"points": [{"severity": "high", "point": "无限责任",
                            "evidence_quote": RAW[:30], "reason": "风险"}]}
        summary = augment(_base_summary(), RAW,
                          transport=_fake_transport(high))
        assert ar.AI_CODE in {h["rule_code"] for h in summary["hits"]}
        assert summary["overall_risk_level"] == "high"
        assert any("[AI]" in h["rule_name"] for h in summary["hits"])
        # 原 low 结论不会被 AI 点降到更低；只会升不会降
        assert summary["focus_points"][-1] == "风险"

    def test_llm_unavailable_silent_fallback(self) -> None:
        def boom(*a, **k):
            raise LLMUnavailable("down")

        summary = augment(_base_summary(), RAW,
                          transport=SimpleNamespace(chat=boom))
        assert summary == _base_summary()   # 原样返回

    def test_malformed_json_silent_fallback(self) -> None:
        msg = SimpleNamespace(chat=lambda *a, **k: {"content": "不是JSON"})
        assert augment(_base_summary(), RAW, transport=msg) == _base_summary()

    def test_short_text_skips(self) -> None:
        assert augment(_base_summary(), "太短", transport=None) == _base_summary()

    def test_disabled_flag_silent(self, monkeypatch) -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "ai_review_enabled", False)
        summary = augment(_base_summary(), RAW,
                          transport=SimpleNamespace(chat=lambda *a, **k: {"content": "{}"}))
        assert summary == _base_summary()


def _fake_transport(payload: dict):
    class T:
        def chat(self, messages, tools=None, *, channel="json"):
            return {"content": json.dumps(payload, ensure_ascii=False)}
    return T()


from app.core.config import get_settings  # noqa: E402


class TestToolIntegration:
    def test_run_rules_tool_merges_ai_layer(self, db_session: Session) -> None:
        seed_rules(db_session)
        from app.models import ApprovalTask, ContractParse

        task = ApprovalTask(approval_code="T-AI-1", approval_title="t",
                            applicant_name="a", instance_id="inst-001")
        db_session.add(task)
        db_session.flush()
        db_session.add(ContractParse(task_id=task.id, raw_text=RAW, parse_status="done"))
        db_session.commit()

        high = {"points": [{"severity": "high", "point": "单方解约权",
                            "evidence_quote": "甲方有权随时解除", "reason": "显失公平"}]}
        import app.services.ai_reviewer as ar

        original = ar._transport
        try:
            ar._transport = lambda: _fake_transport(high)  # type: ignore[assignment]
            from app.services.agent_loop import RunController
            from app.tools_registry import RunContext, execute_tool

            ctx = RunContext(db=db_session, task=task)
            execute_tool(ctx, "run_contract_rules", {})
            codes = {h["rule_code"] for h in (ctx.rules_summary or {}).get("hits", [])}
            assert AI_CODE in codes
            assert (ctx.rules_summary or {}).get("overall_risk_level") == "high"
        finally:
            ar._transport = original
