"""引擎选择器钉死测试（回归锁）。

背景：某夜坏的半成品改动被 git checkout 回滚时，几乎把 V2 引擎接线一起抹掉。
本文件把「分支主线=LangChain 引擎、legacy 仅显式开关可切」的选择器契约锁进单测，
今后任何回滚都必须显式面对这里的红灯，而不是悄悄丢掉主线。
"""
from __future__ import annotations

import app.services.lc_agent as lc_module
from app.services import engine


def _make_task(db_session):
    from tests.factory import make_form

    return make_form(db_session)


def test_langchain_is_default_and_receives_task(db_session, monkeypatch):
    monkeypatch.delenv("AGENT_ENGINE", raising=False)

    seen = {}

    def fake_run_lc(db, task, *, dry_run=False):
        seen["task_id"] = task.id
        seen["dry_run"] = dry_run
        return {"status": "succeeded", "steps": 0}

    monkeypatch.setattr(lc_module, "run_lc", fake_run_lc)

    out = engine.run_full_cycle(db_session, _make_task(db_session), dry_run=True)

    assert out == {"status": "succeeded", "steps": 0}
    assert seen["task_id"] and seen["dry_run"] is True


def test_legacy_switch_still_works(db_session, monkeypatch):
    import app.services.agent_loop as agent_loop_module

    class _FakeRun:
        status = "ran"
        steps_used = 3
        channel = "deterministic"
        fallback_kind = ""

    class _FakeController:
        def __init__(self, db, task, *, dry_run=False):
            self.task = task

        def start(self):
            return _FakeRun()

    monkeypatch.setenv("AGENT_ENGINE", "legacy")
    monkeypatch.setattr(agent_loop_module, "RunController", _FakeController)

    out = engine.run_full_cycle(db_session, _make_task(db_session))

    assert out["status"] == "ran"
    assert out["channel"] == "deterministic"
