"""批量送审的真实进度：前端必须知道 GPU 逐张处理的账本，不许假「执行完毕」。"""
from __future__ import annotations


def test_batch_reports_progress_through_registry(client, db_session, monkeypatch):
    from tests.factory import make_form

    import app.services.lc_agent as lc_module

    monkeypatch.setenv("AGENT_ENGINE", "langchain")
    monkeypatch.setattr(lc_module, "run_lc",
                        lambda db, task, *, dry_run=False:
                        {"status": "succeeded", "steps": 0, "trace": []})

    t1 = make_form(db_session, code="AP-B-001")
    t2 = make_form(db_session, code="AP-B-002")

    resp = client.post("/app/batch_review",
                       json={"task_ids": [t1.id, t2.id]})
    assert resp.status_code == 200, resp.text
    bid = resp.json()["batch_id"]
    assert bid

    status = client.get(f"/app/batch/{bid}").json()
    assert status["total"] == 2
    assert status["done"] + status["skipped"] >= 2   # TestClient 同步跑完工人

    unknown = client.get("/app/batch/no-such-id")
    assert unknown.status_code == 404
