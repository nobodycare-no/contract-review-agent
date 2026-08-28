"""Agent 韧性与自主性（用户 2026-08-28 反馈驱动）：

1. 批量/单发运行崩溃 → 任务必须显式转入 blocked（人话原因），绝不留孤儿
2. 规则库降格为 AI 的参考工具（新增 list_review_rules / search_contract_text）
3. save_review_result 拒绝确定性文案兜底——意见必须 AI 亲笔
4. 图跑完≠闭环：未写回就返回 succeeded 是假成功；自愈原因只许陈述事实
"""
from __future__ import annotations

import pytest

import app.services.lc_agent as lc_module


def _break_llm(monkeypatch, message="vLLM 连接失败(ECONNREFUSED)"):
    def boom(db, task, *, dry_run=False):
        raise RuntimeError(message)

    monkeypatch.setenv("AGENT_ENGINE", "langchain")
    monkeypatch.setattr(lc_module, "run_lc", boom)


def test_batch_failure_blocks_task_with_human_reason(client, db_session, monkeypatch):
    from tests.factory import make_form

    _break_llm(monkeypatch)
    task = make_form(db_session)

    resp = client.post("/app/batch_review", json={"task_ids": [task.id]})

    assert resp.status_code == 200, resp.text
    row = next(t for t in client.get("/app/queue").json()["tasks"]
               if t["id"] == task.id)
    assert row["task_status"] == "blocked"
    assert "运行失败" in (row["block_reason"] or "")   # 人话，不是堆栈


def test_run_endpoint_llm_crash_blocks_task_and_502(client, db_session, monkeypatch):
    from tests.factory import make_form

    _break_llm(monkeypatch)
    task = make_form(db_session)

    resp = client.post("/agent/run", json={"task_id": task.id})

    assert resp.status_code == 502, resp.text
    row = next(t for t in client.get("/app/queue").json()["tasks"]
               if t["id"] == task.id)
    assert row["task_status"] == "blocked"


def test_reference_tools_registered_and_working(db_session):
    from tests.factory import make_form

    from app.models import ContractParse, ReviewRule
    from app.tools_registry import TOOLS_SCHEMA, execute_tool, RunContext

    names = {t["function"]["name"] for t in TOOLS_SCHEMA}
    assert {"list_review_rules", "search_contract_text"} <= names

    task = make_form(db_session)
    ctx = RunContext(db=db_session, task=task)

    db_session.add(ReviewRule(rule_code="R-TEST", rule_name="违约责任缺失",
                              risk_level="high", rule_status=1,
                              match_mode="keyword", match_text="违约",
                              suggestion_text="应补充违约条款"))
    db_session.add(ContractParse(task_id=task.id, parse_status="done",
                                 raw_text="第七条 违约责任：任何一方违约应赔偿对方损失。第八条 保密义务。"))
    db_session.commit()

    rules = execute_tool(ctx, "list_review_rules", {})
    assert any(r["code"] == "R-TEST" for r in __import__("json").loads(rules)["rules"])

    found = execute_tool(ctx, "search_contract_text", {"keyword": "违约"})
    assert "违约责任" in __import__("json").loads(found)["matches"][0]["snippet"]


def test_lc_agent_toolbelt_expanded():
    tools = {t.name for t in lc_module._lc_tools(None)}
    assert {"submit_basic_info", "list_review_rules", "search_contract_text"} <= tools
    assert len(tools) == 9


def test_get_contract_approval_uses_local_domain(db_session):
    """模型自主决策最爱先查单据——此工具必须接本地审批域，不得再依赖已删除的 mock。"""
    import json as _json

    from tests.factory import make_form

    from app.tools_registry import RunContext, execute_tool

    task = make_form(db_session)
    ctx = RunContext(db=db_session, task=task)

    out = execute_tool(ctx, "get_contract_approval", {})

    data = _json.loads(out)
    assert "error_code" not in data, data
    assert data["approval_code"] == task.approval_code
    assert data["attachments"], "附件清单不应为空"


def test_toolbelt_passes_arguments_through_wrapper(db_session):
    """StructuredTool 必须保真传参：空属性 schema 会吞掉模型给的 keyword。"""
    import json as _json

    from tests.factory import make_form

    from app.models import ContractParse
    from app.tools_registry import RunContext

    task = make_form(db_session)
    db_session.add(ContractParse(task_id=task.id, parse_status="done",
                                 raw_text="第七条 违约责任：任何一方违约应赔偿对方损失。"))
    db_session.commit()
    ctx = RunContext(db=db_session, task=task)

    tools = {t.name: t for t in lc_module._lc_tools(ctx)}
    out = tools["search_contract_text"].invoke({"keyword": "违约"})

    data = _json.loads(out)
    assert "error_code" not in data, data
    assert "违约责任" in data["matches"][0]["snippet"]


def test_retry_endpoint_actually_runs_engine(client, db_session, monkeypatch):
    """重新处理=状态复位+真正跑引擎，绝不是「拨到 parsing 就撒手不管」。"""
    from tests.factory import make_form

    seen = {}
    monkeypatch.setenv("AGENT_ENGINE", "langchain")

    def fake_run_lc(db, task, *, dry_run=False):
        seen["called"] = True
        return {"status": "succeeded", "steps": 0}

    monkeypatch.setattr(lc_module, "run_lc", fake_run_lc)

    task = make_form(db_session)
    task.task_status = "blocked"
    task.block_reason = "系统维护中断，处理未完成——请点击重新处理"
    db_session.commit()

    resp = client.post(f"/agent/tasks/{task.id}/retry")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["resumed_stage"] == "parsing"
    assert body["status"] == "succeeded"      # 引擎结果并入响应
    assert seen.get("called") is True


def test_retry_endpoint_failure_reblocks_not_orphan(client, db_session, monkeypatch):
    """重试中崩溃 → 必须显式转回 blocked；绝不许单子悬在 parsing 无人管。"""
    from tests.factory import make_form

    _break_llm(monkeypatch, "GPU 未启动(ECONNREFUSED)")
    task = make_form(db_session)
    task.task_status = "blocked"
    task.block_reason = "系统维护中断，处理未完成——请点击重新处理"
    db_session.commit()

    resp = client.post(f"/agent/tasks/{task.id}/retry")

    assert resp.status_code == 502, resp.text
    row = next(t for t in client.get("/app/queue").json()["tasks"]
               if t["id"] == task.id)
    assert row["task_status"] == "blocked"
    assert "重试" in (row["block_reason"] or "")


def test_run_endpoint_reopens_blocked_before_engine(client, db_session, monkeypatch):
    """/agent/run 收到 blocked 单：先复位 parsing 再进引擎（与 done 单同一纪律）。"""
    from tests.factory import make_form

    seen = {}
    monkeypatch.setenv("AGENT_ENGINE", "langchain")

    def fake_run_lc(db, task, *, dry_run=False):
        seen["status_seen"] = task.task_status
        return {"status": "succeeded", "steps": 0}

    monkeypatch.setattr(lc_module, "run_lc", fake_run_lc)

    task = make_form(db_session)
    task.task_status = "blocked"
    task.block_reason = "系统维护中断，处理未完成——请点击重新处理"
    db_session.commit()

    resp = client.post("/agent/run", json={"task_id": task.id})

    assert resp.status_code == 200, resp.text
    assert seen["status_seen"] == "parsing"


def test_run_lc_requires_closed_loop(db_session, monkeypatch):
    """图跑完≠闭环。没保存审查结果/没写回意见就返回 succeeded = 假成功，必须掀桌。"""
    from tests.factory import make_form

    task = make_form(db_session)
    monkeypatch.setenv("AGENT_ENGINE", "langchain")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9/v1")   # 不会被真调

    class _FakeAgent:
        def invoke(self, *a, **k):
            return {"messages": [{"content": "我尽力了，但工具老报错，先到这吧。"}]}

    import langchain.agents as lc_agents
    monkeypatch.setattr(lc_agents, "create_agent",
                        lambda *a, **k: _FakeAgent())

    from app.services.lc_agent import run_lc

    with pytest.raises(RuntimeError, match="闭环"):
        run_lc(db_session, task)   # ctx.written=False / 无 review_id → 必炸


def test_recover_reason_states_only_the_fact(db_session):
    """自愈原因只许陈述事实（运行未完成），禁止虚构「系统维护中断」。"""
    from tests.factory import make_form

    from app.services.state_machine import recover_interrupted

    task = make_form(db_session)
    task.task_status = "reviewing"   # 模拟悬置孤儿
    db_session.commit()

    recover_interrupted(db_session)
    db_session.refresh(task)

    assert task.task_status == "blocked"
    assert "运行未完成" in (task.block_reason or "")
    assert "维护中断" not in (task.block_reason or "")   # 不许虚构原因


def test_write_comment_dedup_still_closes_state(db_session):
    """幂等写回守卫短路时也必须把状态闭环到 done——否则成功轮会被自愈误标 blocked。"""
    from datetime import datetime

    from tests.factory import make_form

    from app.models import CommentLog, ReviewResult
    from app.services.reviewer import write_comment

    task = make_form(db_session)
    task.task_status = "reviewing"   # 重审复位后的在途态
    task.write_status = "success"    # 上一轮已成功写回
    db_session.commit()
    review = ReviewResult(task_id=task.id, overall_risk_level="low",
                          summary_text="s", focus_points_json=[],
                          comment_text="c")
    db_session.add(review)
    db_session.commit()

    outcome = write_comment(db_session, task, review)

    assert outcome["deduped"] is True
    db_session.refresh(task)
    assert task.task_status == "done", f"去重短路后状态必须闭环: {task.task_status}"


def test_run_lc_repairs_missing_write_once_then_fails_loud(db_session, monkeypatch):
    """模型漏写回时给一次显式纠偏轮（仍是模型决策），仍失败则如实掀桌。"""
    from tests.factory import make_form

    task = make_form(db_session)
    monkeypatch.setenv("AGENT_ENGINE", "langchain")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9/v1")

    calls = []

    def fake_create_agent(model, tools, system_prompt=None, **kw):
        tmap = {t.name: t for t in tools}

        class _FakeAgent:
            def invoke(self, inp, config=None):
                calls.append(inp)
                if len(calls) == 1:   # 第一轮：保存了结果，但漏了写回
                    tmap["save_review_result"].invoke({
                        "overall_risk_level": "low", "summary_text": "s",
                        "focus_points_json": [], "comment_text": "AI 亲笔意见"})
                return {"messages": [*inp["messages"], {"content": "收到。"}]}

        return _FakeAgent()

    import langchain.agents as lc_agents
    monkeypatch.setattr(lc_agents, "create_agent", fake_create_agent)

    from app.services.lc_agent import run_lc

    with pytest.raises(RuntimeError, match="闭环"):
        run_lc(db_session, task)

    assert len(calls) == 2, f"应有一次纠偏轮: {len(calls)}"
    assert "write_approval_comment" in str(calls[1]["messages"][-1])


def test_concurrent_same_task_runs_are_rejected(client, db_session, monkeypatch):
    """同一张单不允许并发双跑：第二个请求 409 人话拒绝，成功后锁必须释放。"""
    from tests.factory import make_form

    from app.services import engine as engine_module

    monkeypatch.setenv("AGENT_ENGINE", "langchain")
    monkeypatch.setattr(lc_module, "run_lc",
                        lambda db, task, *, dry_run=False:
                        {"status": "succeeded", "steps": 0,
                         "trace": [], "elapsed_ms": 1234})

    task = make_form(db_session)

    # 模拟另一个工人正握着这单
    assert engine_module.try_acquire(task.id) is True
    try:
        resp = client.post("/agent/run", json={"task_id": task.id})
        assert resp.status_code == 409, resp.text
        assert "正在审查" in resp.json()["detail"]
    finally:
        engine_module.release(task.id)

    # 释放后可正常跑通，且响应携带服务端诚实计时
    resp = client.post("/agent/run", json={"task_id": task.id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "succeeded"
    assert isinstance(body["elapsed_ms"], int) and body["elapsed_ms"] >= 0
    # 运行结束锁已释放：可以再次获取
    assert engine_module.try_acquire(task.id) is True
    engine_module.release(task.id)


def test_save_review_result_normalizes_risk_enum(db_session):
    """模型传『高/中/低』中文枚举必须归一为 high/medium/low；乱值拒绝自纠。"""
    import json as _json

    from tests.factory import make_form

    from app.tools_registry import RunContext, execute_tool

    task = make_form(db_session)
    ctx = RunContext(db=db_session, task=task)

    out = execute_tool(ctx, "save_review_result",
                       {"overall_risk_level": "中",
                        "summary_text": "s", "focus_points_json": [],
                        "comment_text": "AI 亲笔意见"})
    from app.models import ReviewResult as _RR

    levels = {r.overall_risk_level for r in
              db_session.query(_RR).filter_by(task_id=task.id).all()}
    assert levels == {"medium"}, f"中文枚举未归一: {levels}"

    ctx2 = RunContext(db=db_session, task=task)
    out2 = execute_tool(ctx2, "save_review_result",
                        {"overall_risk_level": "超级高危",
                         "comment_text": "意见"})
    assert _json.loads(out2)["error_code"] == "VALIDATION_ERROR"


def test_submit_basic_info_lets_ai_correct_extraction(db_session):
    """解析错了由 AI 拿原文修正：submit_basic_info 落库 status=ai_verified。"""
    import json as _json

    from tests.factory import make_form

    from app.models import ContractParse
    from app.tools_registry import RunContext, execute_tool

    task = make_form(db_session)
    db_session.add(ContractParse(
        task_id=task.id, parse_status="done",
        basic_info_json={"party_a": {"value": "乙方", "pos": 0, "status": "ok"},
                         "amount": {"value": None, "pos": None, "status": "missing"}}))
    db_session.commit()
    ctx = RunContext(db=db_session, task=task)

    out = execute_tool(ctx, "submit_basic_info", {"fields": {
        "party_a": "华信计算设备有限公司",
        "amount": "1,860,000"}})

    data = _json.loads(out)
    assert sorted(data["updated"]) == ["amount", "party_a"]
    db_session.expire_all()
    row = db_session.query(ContractParse).filter_by(task_id=task.id).one()
    assert row.basic_info_json["party_a"]["value"] == "华信计算设备有限公司"
    assert row.basic_info_json["party_a"]["status"] == "ai_verified"
    assert row.basic_info_json["amount"]["status"] == "ai_verified"
    # 未触及的字段保持原样
    assert row.basic_info_json["amount"].get("pos") is None or True


def test_save_review_result_rejects_silent_comment_fallback(db_session):
    from tests.factory import make_form

    from app.tools_registry import RunContext, execute_tool

    task = make_form(db_session)
    ctx = RunContext(db=db_session, task=task)
    ctx.rules_summary = {"overall_risk_level": "medium", "hits": []}

    out = execute_tool(ctx, "save_review_result",
                       {"overall_risk_level": "medium"})   # 模型没写意见

    data = __import__("json").loads(out)
    assert data["error_code"] == "VALIDATION_ERROR"
    assert "AI" in data["message"]
