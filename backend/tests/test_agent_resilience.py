"""Agent 韧性与自主性（用户 2026-08-28 反馈驱动）：

1. 批量/单发运行崩溃 → 任务必须显式转入 blocked（人话原因），绝不留孤儿
2. 规则库降格为 AI 的参考工具（新增 list_review_rules / search_contract_text）
3. save_review_result 拒绝确定性文案兜底——意见必须 AI 亲笔
"""
from __future__ import annotations

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
    assert {"list_review_rules", "search_contract_text"} <= tools
    assert len(tools) == 8


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
