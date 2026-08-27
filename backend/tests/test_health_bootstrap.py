"""T2 骨架行为测试：健康组件面 / metrics 暴露 / 提示词注册表 / 规则种子幂等。"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.prompts import active_version, render
from app.tools.bootstrap import seed_rules


def test_health_reports_components(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in {"ok", "degraded"}
    comps = body["components"]
    assert set(comps) == {"mysql", "mock", "llm"}
    assert isinstance(comps["mysql"]["ok"], bool)
    assert comps["llm"]["ok"] is None and comps["llm"]["note"] == "not_configured"


def test_metrics_endpoint_exposes_cra_families(client: TestClient) -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    for family in ("cra_runs_total", "cra_llm_calls_total", "cra_tool_calls_total",
                   "cra_fallback_total", "cra_blocked_total",
                   "cra_run_latency_seconds", "cra_circuit_state"):
        assert family in text, f"缺少指标族 {family}"


def test_prompt_registry_versions_and_render() -> None:
    assert active_version("agent_system") == "v1"
    rendered = render("parse_enhance", text="甲乙双方…")
    assert "甲乙双方…" in rendered
    # 未提供占位符时保留原样，便于排查
    keep = render("comment_polish")
    assert "{data}" in keep


def test_seed_rules_idempotent_with_ai_anchor(db_session: Session) -> None:
    """12 行 = 11 条引擎规则 + AI_DISCRETIONARY 落库锚点(status=0 不参与匹配)。"""
    from app.models import ReviewRule

    created, updated = seed_rules(db_session)
    assert created == 12 and updated == 0
    created2, updated2 = seed_rules(db_session)
    assert (created2, updated2) == (0, 0), "二次种子应完全幂等"
    total = db_session.query(ReviewRule).count()
    active = db_session.query(ReviewRule).filter(ReviewRule.rule_status == 1).count()
    assert total == 12 and active == 11


def test_agent_run_row_roundtrip(db_session: Session) -> None:
    """第九表基本读写：快照 JSON 与预算字段。"""
    from app.models import AgentRun, ApprovalTask

    task = ApprovalTask(approval_code="T-001", approval_title="t", applicant_name="a",
                        instance_id="inst-001")
    db_session.add(task)
    db_session.commit()

    run = AgentRun(task_id=task.id, channel="native", dry_run=0,
                   messages_json=[{"role": "user", "content": "go"}])
    db_session.add(run)
    db_session.commit()

    loaded = db_session.query(AgentRun).one()
    assert loaded.status == "running"
    assert loaded.messages_json[0]["content"] == "go"
    assert loaded.steps_used == 0 and loaded.fallback_kind is None
