"""T6a RunController 全分支：native 闭环 / 预算优雅终结 / 确定性降级 / dry-run / 断点恢复 / 协议解析。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.obs import reset_circuit_breaker
from app.models import AgentRun, ApprovalTask, ReviewResult
from app.services.agent_loop import RunController
from app.tools_registry import parse_protocol_line


def _tc(name: str, args: dict | None = None) -> dict:
    import json

    return {"role": "assistant", "content": None, "_usage":
            {"prompt_tokens": 100, "completion_tokens": 20},
            "tool_calls": [{"id": f"c{name}", "type": "function",
                            "function": {"name": name,
                                         "arguments": json.dumps(args or {})}}]}


class ScriptedLLM:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)

    def chat(self, messages, tools=None, *, channel="native"):
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return {**item}


@pytest.fixture()
def fake_mock(monkeypatch):
    """回写捕获器：真实本地回写之上包一层记录（字典键与旧断言兼容）。"""
    from tests.factory import post_spy

    return {"posted": post_spy(monkeypatch)}


@pytest.fixture(autouse=True)
def _reset_breaker():
    reset_circuit_breaker()
    yield
    reset_circuit_breaker()


@pytest.fixture()
def force_native(monkeypatch):
    """跳过探针消耗脚本：直接锁定 native 通道。"""
    import app.services.llm_client as lc

    monkeypatch.setattr(lc, "probe_native_tools", lambda transport=None: True)


def _make_task(db: Session, code="AP-Z-001"):
    """经真实工厂建单（附件落盘可解析）。"""
    from tests.factory import make_form

    return make_form(db, code=code, title="采购合同审批")


HAPPY_NATIVE = [_tc("download_contract_attachment"),
                _tc("parse_contract_document"),
                _tc("run_contract_rules"),
                _tc("save_review_result"),
                _tc("write_approval_comment")]

JSON_HAPPY = [{"role": "assistant", "content": '{"tool":"download_contract_attachment","args":{}}',
               "_usage": {"prompt_tokens": 80, "completion_tokens": 15}},
              {"role": "assistant", "content": '{"tool":"parse_contract_document","args":{}}',
               "_usage": {"prompt_tokens": 80, "completion_tokens": 15}},
              {"role": "assistant", "content": '{"tool":"run_contract_rules","args":{}}',
               "_usage": {"prompt_tokens": 80, "completion_tokens": 15}},
              {"role": "assistant", "content": '{"tool":"save_review_result","args":{}}',
               "_usage": {"prompt_tokens": 80, "completion_tokens": 15}},
              {"role": "assistant", "content": '{"tool":"write_approval_comment","args":{}}',
               "_usage": {"prompt_tokens": 80, "completion_tokens": 15}}]


class TestNativeHappyPath:
    def test_full_loop_succeeds_without_fallback(self, client: TestClient,
                                                 db_session: Session, fake_mock,
                                                 force_native) -> None:
        from app.tools.bootstrap import seed_rules

        seed_rules(db_session)
        task = _make_task(db_session)

        controller = RunController(db_session, task, transport=ScriptedLLM(HAPPY_NATIVE))
        run = controller.start()

        assert run.status == "succeeded"
        assert run.channel == "native"
        assert run.fallback_kind is None
        assert run.steps_used == len(HAPPY_NATIVE)
        assert run.prompt_version == "v1" and run.model_name
        assert fake_mock["posted"] and "总风险等级" in fake_mock["posted"][0]

        task = db_session.query(ApprovalTask).filter_by(id=task.id).one()
        assert task.task_status == "done" and task.write_status == "success"
        assert any(t["tool"] == "run_contract_rules" for t in controller.ctx.trace)


class TestBudgetGracefulFinalize:
    def test_step_budget_forces_write(self, client: TestClient, db_session: Session,
                                      fake_mock, monkeypatch, force_native) -> None:
        from app.tools.bootstrap import seed_rules

        seed_rules(db_session)
        settings = get_settings()
        monkeypatch.setattr(settings, "agent_max_steps", 2)

        endless = [_tc("list_pending_contract_approvals"),
                   _tc("list_pending_contract_approvals")]
        task = _make_task(db_session)
        controller = RunController(db_session, task, transport=ScriptedLLM(endless))
        run = controller.start()

        assert run.status == "succeeded"
        assert run.fallback_kind == "budget_steps"
        assert run.steps_used == 2
        assert fake_mock["posted"], "预算触顶必须兜底回写"
        assert "总风险等级" in fake_mock["posted"][0]


class TestDeterministicFallback:
    def test_no_transport_runs_deterministic(self, client: TestClient,
                                             db_session: Session, fake_mock) -> None:
        from app.tools.bootstrap import seed_rules

        seed_rules(db_session)
        task = _make_task(db_session)
        controller = RunController(db_session, task, transport=None)
        run = controller.start()

        assert run.status == "succeeded"
        assert run.channel == "deterministic"
        assert run.fallback_kind == "llm_down"
        assert len(fake_mock["posted"]) == 1
        assert controller.ctx.written


class TestDryRun:
    def test_dry_run_never_posts_comment(self, db_session: Session, fake_mock) -> None:
        from app.tools.bootstrap import seed_rules

        seed_rules(db_session)
        task = _make_task(db_session)
        controller = RunController(db_session, task, dry_run=True, transport=None)
        run = controller.start()

        assert run.status == "succeeded" and run.dry_run == 1
        assert fake_mock["posted"] == []
        task = db_session.query(ApprovalTask).filter_by(id=task.id).one()
        assert task.task_status == "reviewing"
        assert task.write_status == "not_written"
        assert db_session.query(ReviewResult).filter_by(task_id=task.id).count() == 1


class TestResume:
    def test_resume_from_snapshot_completes(self, client: TestClient,
                                            db_session: Session, fake_mock) -> None:
        from app.tools.bootstrap import seed_rules

        seed_rules(db_session)
        task = _make_task(db_session)
        crashed = AgentRun(task_id=task.id, status="running", channel="json",
                           messages_json=[{"role": "system", "content": "s"},
                                          {"role": "user", "content": "u"}])
        db_session.add(crashed)
        db_session.commit()

        from app.services.agent_loop import RunController

        controller = RunController(db_session, task,
                                   transport=ScriptedLLM(JSON_HAPPY))
        run = controller.resume(crashed.id)

        assert run.status == "succeeded"
        assert run.channel == "json"
        assert fake_mock["posted"] and "总风险等级" in fake_mock["posted"][0]


class TestProtocolParser:
    def test_tool_line(self) -> None:
        tool, args = parse_protocol_line('前缀 {"tool":"run_contract_rules","args":{"case_id":3}} 后缀')
        assert tool == "run_contract_rules" and args == {"case_id": 3}

    def test_final_line(self) -> None:
        tool, obj = parse_protocol_line('{"final":"已完成"}')
        assert tool is None and obj["final"] == "已完成"

    def test_garbage_returns_none(self) -> None:
        assert parse_protocol_line("模型闲聊，没有 JSON") == (None, {})
