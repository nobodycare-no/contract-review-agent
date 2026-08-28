"""已审查合同可再次审查（回归需求）：done 不再是状态机死胡同。

用户规则：审查完成的合同必须允许一键重跑（合同改版后复检是 C 端刚需），
而不是把「已完成」焊死。
"""
from __future__ import annotations


def test_done_task_retry_transitions_to_parsing(db_session):
    from tests.factory import make_form

    from app.services.state_machine import retry_task

    task = make_form(db_session)
    task.task_status = "done"
    db_session.commit()

    stage = retry_task(db_session, task)

    assert stage == "parsing"
    db_session.refresh(task)
    assert task.task_status == "parsing"


def test_agent_run_reruns_done_task(client, db_session, monkeypatch):
    """/agent/run 收到 done 单：先复位 parsing，再交给引擎（车道无关）。"""
    from tests.factory import make_form

    import app.services.lc_agent as lc_module

    seen = {}
    monkeypatch.setenv("AGENT_ENGINE", "langchain")

    def fake_run_lc(db, task, *, dry_run=False):
        seen["status_seen"] = task.task_status
        return {"status": "succeeded", "steps": 0}

    monkeypatch.setattr(lc_module, "run_lc", fake_run_lc)

    task = make_form(db_session)   # 工厂已重挂回 db_session
    task.task_status = "done"
    db_session.commit()

    resp = client.post("/agent/run", json={"task_id": task.id})

    assert resp.status_code == 200, resp.text
    assert seen["status_seen"] == "parsing"   # 引擎看到的已是复位后的单子


def test_agent_run_persists_tool_trace(client, db_session, monkeypatch):
    """引擎工具轨迹必须落库为 TaskLog——留痕时间线的数据源，不得静默丢弃。"""
    from tests.factory import make_form

    import app.services.lc_agent as lc_module

    monkeypatch.setenv("AGENT_ENGINE", "langchain")

    def fake_run_lc(db, task, *, dry_run=False):
        return {"status": "succeeded", "steps": 2,
                "trace": [{"tool": "download_contract_attachment", "outcome": "ok"},
                          {"tool": "run_contract_rules", "outcome": "PARSE_EMPTY"}]}

    monkeypatch.setattr(lc_module, "run_lc", fake_run_lc)

    task = make_form(db_session)
    resp = client.post("/agent/run", json={"task_id": task.id, "dry_run": True})

    assert resp.status_code == 200, resp.text
    assert resp.json()["trace"] == [
        {"tool": "download_contract_attachment", "outcome": "ok"},
        {"tool": "run_contract_rules", "outcome": "PARSE_EMPTY"}]

    logs = client.get(f"/agent/tasks/{task.id}/logs").json()["logs"]
    tool_logs = [l for l in logs if l["type"] == "tool"]
    assert len(tool_logs) == 2
    assert "download_contract_attachment → ok" == tool_logs[0]["content"]
