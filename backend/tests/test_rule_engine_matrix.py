"""T4 规则引擎矩阵：三模式语义 / 阈值 / 错误隔离 / 汇总等级 / 持久化与重跑。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.models import RuleHit
from app.services.rule_engine import (evaluate_rules, run_task_rules, summarize)
from app.services.rule_seed import SEED_RULES
from app.tools.bootstrap import seed_rules


def _rule(code="R1", name="r", level="medium", mode="keyword", match="a,b",
          suggestion="建议"):
    return SimpleNamespace(id=hash(code) % 1000, rule_code=code, rule_name=name,
                           risk_level=level, rule_status=1, match_mode=mode,
                           match_text=match, suggestion_text=suggestion)


class TestKeywordMode:
    def test_any_keyword_hits_with_position(self) -> None:
        ev = evaluate_rules([_rule(match="保密,机密")], "双方负有保密义务……") [0]
        assert ev.hit_status == "hit"
        assert ev.evidence_position.isdigit()

    def test_no_keyword_misses(self) -> None:
        ev = evaluate_rules([_rule(match="保密,机密")], "本合同不含敏感条款")[0]
        assert ev.hit_status == "miss"


class TestAbsenceMode:
    def test_all_missing_hits(self) -> None:
        ev = evaluate_rules([_rule(mode="absence", match="违约,赔偿")], "全文毫无风险条款") [0]
        assert ev.hit_status == "hit"
        assert "违约" in ev.evidence_text and "赔偿" in ev.evidence_text

    def test_partial_present_only_misses(self) -> None:
        """absence 是全缺失探测：部分出现即不命中。"""
        ev = evaluate_rules([_rule(mode="absence", match="违约,赔偿")], "有赔偿责任约定")[0]
        assert ev.hit_status == "miss"


class TestRegexMode:
    def test_numeric_above_threshold_hits(self) -> None:
        rule = _rule(code="PAY_ADVANCE_HIGH", mode="regex",
                     match=r"预付[^。]{0,14}?([0-9]+)\s*%")
        ev = evaluate_rules([rule], "预付款比例为合同总金额的 50%。")[0]
        assert ev.hit_status == "hit"

    def test_numeric_below_threshold_misses(self) -> None:
        rule = _rule(code="PAY_ADVANCE_HIGH", mode="regex",
                     match=r"预付[^。]{0,14}?([0-9]+)\s*%")
        ev = evaluate_rules([rule], "预付款比例为合同总金额的 20%。")[0]
        assert ev.hit_status == "miss"

    def test_non_registered_regex_plain_hit(self) -> None:
        rule = _rule(code="ANY_X", mode="regex", match=r"(?:向|由)(甲方|乙方)所在地(?:人民法院|法院)")
        ev = evaluate_rules([rule], "可向甲方所在地人民法院提起诉讼")[0]
        assert ev.hit_status == "hit"

    def test_invalid_regex_is_error_row_not_exception(self) -> None:
        rule = _rule(mode="regex", match="(未闭合[")
        ev = evaluate_rules([rule], "任意文本")[0]
        assert ev.hit_status == "error" and "编译失败" in ev.evidence_text


class TestSummarize:
    def test_overall_takes_max_and_sorted(self) -> None:
        def _hit(code, level, suggestion):
            return SimpleNamespace(hit_status="hit", risk_level=level, rule_code=code,
                                   rule_name=f"n-{code}", suggestion_text=suggestion,
                                   evidence_text="evi", evidence_position="0")

        evals = [
            _hit("A", "low", "s1"),
            _hit("B", "high", "s2"),
            _hit("C", "medium", "s3"),
            SimpleNamespace(hit_status="miss", risk_level="high", rule_code="D",
                            rule_name="n-D", suggestion_text="x",
                            evidence_text="", evidence_position=""),
        ]
        summary = summarize(evals)
        assert summary["overall_risk_level"] == "high"
        assert summary["overall_risk_label"] == "高"
        assert [h["rule_code"] for h in summary["hits"]] == ["B", "C", "A"]
        assert summary["focus_points"] == ["s2", "s3", "s1"]
        assert summary["stats"] == {"evaluated": 4, "hit": 3, "miss": 1, "error": 0}

    def test_empty_hits_means_low(self) -> None:
        assert summarize([])["overall_risk_level"] == "low"


class TestPersistence:
    def test_run_task_rules_persists_all_outcomes_and_reruns_clean(self, db_session: Session):
        from app.models import AgentRun, ApprovalTask, ContractParse

        seed_rules(db_session)

        task = ApprovalTask(approval_code="T-RULE-1", approval_title="采购",
                            applicant_name="王", instance_id="inst-001")
        db_session.add(task)
        db_session.flush()
        text = ("合同编号：HT-2026-0301\n甲方（采购方）：XX科技有限公司（统一社会信用代码：91310000MA1FL8X20A）\n"
                "合同总金额为人民币 1,860,000 元，含税。\n预付款比例为合同总金额的 50%。\n"
                "剩余货款于到货后 90 个工作日内支付。\n任何一方可向甲方所在地人民法院提起诉讼。")
        db_session.add(ContractParse(task_id=task.id, raw_text=text, parse_status="done"))
        db_session.commit()

        rules = db_session.query(type(task)).one()  # noqa: F841 —— 确保任务落库
        from app.models import ReviewRule

        active = db_session.query(ReviewRule).filter(ReviewRule.rule_status == 1).all()
        summary = run_task_rules(db_session, task.id, active, text)

        # 预期命中矩阵（与 mock inst-001 设计一致）
        hit_codes = {h["rule_code"] for h in summary["hits"]}
        assert {"PAY_ADVANCE_HIGH", "PAY_CYCLE_LONG", "NO_BREACH",
                "JURISDICTION_RISK", "NDA_MISSING", "IP_MISSING",
                "ACCEPTANCE_MISSING"} <= hit_codes
        assert "PARTY_MISSING" not in hit_codes and "AMOUNT_MISSING" not in hit_codes
        assert summary["overall_risk_level"] == "high"

        rows = db_session.query(RuleHit).filter_by(task_id=task.id).all()
        assert len(rows) == len(active)  # 全量落库：hit+miss+error

        # 重跑不累积
        run_task_rules(db_session, task.id, active, text)
        assert db_session.query(RuleHit).filter_by(task_id=task.id).count() == len(active)
