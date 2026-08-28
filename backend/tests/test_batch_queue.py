"""批量审查排队 UX（用户 2026-08-28 反馈驱动）：

1. 点击「批量审查」那一刻，选中的待处理单必须立刻变「排队中」——
   不能等工人跑到才变；用户盯着一批「待处理」会以为没点上。
2. 工人真正开跑某张时 queued→parsing（前端呈现「AI 审查中」），闭环后 done。
3. 批量工人并行执行（BATCH_CONCURRENCY，默认 3）——GLM 单张 3~5 分钟，
   串行 5 张要一刻钟起，用户等不起。
4. 进程重启后，从未开跑的 queued 单回 pending（只陈述事实：还没轮到它）。
"""
from __future__ import annotations


def _stub_run_lc(steps: int = 3):
    def fake(db, task, *, dry_run=False):
        from app.services.state_machine import advance_to

        advance_to(db, task, "done")
        return {"status": "succeeded", "steps": steps, "trace": []}

    return fake


def test_batch_marks_pending_tasks_queued_at_click(client, db_session, monkeypatch):
    """点击瞬间排队：worker 整个被换成不执行的桩，状态也必须已经是 queued。"""
    from tests.factory import make_form

    import app.api.portal as portal_mod

    captured: dict = {}
    monkeypatch.setattr(portal_mod, "_run_batch",
                        lambda bid, ids: captured.update(bid=bid, ids=ids))

    t1 = make_form(db_session, code="AP-Q-001")
    t2 = make_form(db_session, code="AP-Q-002")

    resp = client.post("/app/batch_review", json={"task_ids": [t1.id, t2.id]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["queued"] == 2                       # 响应里带回排队张数
    assert captured["ids"] == [t1.id, t2.id]         # worker 仍被照常调度

    db_session.expire_all()
    assert t1.task_status == "queued" and t2.task_status == "queued"


def test_batch_queue_only_touches_pending(client, db_session, monkeypatch):
    """只把待处理单置排队；需人工处理单保持原状（工人到它时仍走重试语义）。"""
    from tests.factory import make_form

    import app.api.portal as portal_mod
    from app.services.state_machine import block_task

    monkeypatch.setattr(portal_mod, "_run_batch", lambda bid, ids: None)

    t_pending = make_form(db_session, code="AP-Q-101")
    t_blocked = make_form(db_session, code="AP-Q-102")
    block_task(db_session, t_blocked, "LLM_RUN_FAILED", "历史失败")

    resp = client.post("/app/batch_review",
                       json={"task_ids": [t_pending.id, t_blocked.id]})
    assert resp.json()["queued"] == 1

    db_session.expire_all()
    assert t_pending.task_status == "queued"
    assert t_blocked.task_status == "blocked"


def test_batch_worker_flows_queued_to_done(client, db_session, monkeypatch):
    """工人开跑：queued→parsing（「AI 审查中」）→ … → done；账本逐张记 done。"""
    from tests.factory import make_form

    import app.services.lc_agent as lc_module

    monkeypatch.setenv("AGENT_ENGINE", "langchain")
    monkeypatch.setattr(lc_module, "run_lc", _stub_run_lc())

    t1 = make_form(db_session, code="AP-Q-201")
    t2 = make_form(db_session, code="AP-Q-202")

    resp = client.post("/app/batch_review", json={"task_ids": [t1.id, t2.id]})
    bid = resp.json()["batch_id"]

    status = client.get(f"/app/batch/{bid}").json()
    assert status["done"] == 2 and status["skipped"] == 0

    db_session.expire_all()
    assert t1.task_status == "done" and t2.task_status == "done"


def test_batch_runs_tasks_in_parallel(client, db_session, monkeypatch):
    """GLM 单张 3~5 分钟——批量工人必须真并行（峰值并发 ≥2），不是假排队串行跑。"""
    import threading
    import time

    from tests.factory import make_form

    import app.services.lc_agent as lc_module

    monkeypatch.setenv("AGENT_ENGINE", "langchain")
    monkeypatch.setenv("BATCH_CONCURRENCY", "3")

    lock = threading.Lock()
    state = {"active": 0, "max": 0}

    def fake(db, task, *, dry_run=False):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.2)
        with lock:
            state["active"] -= 1
        return _stub_run_lc()(db, task)

    monkeypatch.setattr(lc_module, "run_lc", fake)

    ids = [make_form(db_session, code=f"AP-Q-3{i:02d}").id for i in range(1, 4)]
    resp = client.post("/app/batch_review", json={"task_ids": ids})
    bid = resp.json()["batch_id"]

    status = client.get(f"/app/batch/{bid}").json()
    assert status["done"] == 3
    assert state["max"] >= 2, \
        f"峰值并发 {state['max']}——工人仍在串行，用户要等 N×单张时长"


def test_recovery_heals_queued_to_pending(db_session):
    """重启自愈：从未开跑的 queued 单诚实回到待处理；默认不碰 queued。"""
    from tests.factory import make_form

    from app.services.state_machine import recover_interrupted, transition

    t = make_form(db_session, code="AP-Q-401")
    assert transition(db_session, t, "queued")

    assert recover_interrupted(db_session) == 0        # 默认：批量启动前不洗 queued
    db_session.expire_all()
    assert t.task_status == "queued"

    assert recover_interrupted(db_session, heal_queued=True) >= 1
    db_session.expire_all()
    assert t.task_status == "pending"


def test_batch_rereviews_done_task_with_visible_flow(client, db_session, monkeypatch):
    """批量重审已 done 的单：点击即排队 → parsing → 重新 done，状态全程可见。

    用户实测缺陷：批量里再次审查已完成合同，状态一直钉在「已完成」——
    因为 done 单进批次既不排队也不迁移，工具链的 advance_to 从 done 全是空转，
    重审真实在跑却对用户完全隐形。"""
    from tests.factory import make_form

    import app.services.lc_agent as lc_module
    from app.services.state_machine import transition

    monkeypatch.setenv("AGENT_ENGINE", "langchain")

    seen: dict = {}

    def fake(db, task, *, dry_run=False):
        seen["status_at_run"] = task.task_status   # 工人开跑瞬间的状态
        return _stub_run_lc()(db, task)

    monkeypatch.setattr(lc_module, "run_lc", fake)

    t = make_form(db_session, code="AP-R-001")
    for step in ("queued", "parsing", "reviewing", "done"):
        assert transition(db_session, t, step)
    assert t.task_status == "done"

    resp = client.post("/app/batch_review", json={"task_ids": [t.id]})
    body = resp.json()
    assert body["queued"] == 1, f"done 单应点击即排队：{body}"

    # TestClient 在响应前同步跑完工人——中间态由桩捕获：
    # 走过排队流程则工人开跑时必为 parsing；缺陷未修则是 done 直跑（状态全程钉死已完成）
    assert seen.get("status_at_run") == "parsing", \
        f"工人开跑时状态={seen.get('status_at_run')}——done 单没有经过排队迁移"

    status = client.get(f"/app/batch/{body['batch_id']}").json()
    assert status["done"] == 1
    db_session.expire_all()
    assert t.task_status == "done"   # 重审闭环后再次完成
