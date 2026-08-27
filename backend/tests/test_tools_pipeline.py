"""T5/V1 工具面状态机集成测试：走真实本地审批域（工厂建单），全链路离线回归。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.factory import make_form, post_spy


@pytest.fixture()
def seeded(db_session: Session):
    """经真实 create_form 建两单：带附件(001) + 缺附件(006)；返回回写捕获列表。"""
    make_form(db_session, code="AP-X-001", title="采购合同审批", files=1)
    make_form(db_session, code="AP-X-006", title="缺附件异常单",
              applicant="李梅", files=0)
    return post_spy(pytest.MonkeyPatch())


def _post(client: TestClient, path: str, payload: dict) -> dict:
    resp = client.post(path, json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestFetchDedup:
    def test_queue_view_stable_and_duplicate_code_rejected(
            self, client: TestClient, db_session: Session, seeded) -> None:
        """唯一业务标识去重：①创建层拒绝同 code ②队列视图多次调用零漂移。"""
        from app.services.tool_errors import ToolError

        first = _post(client, "/tools/list_pending", {"limit": 10})
        assert first["data"]["sync"]["total"] == 2

        with pytest.raises(ToolError):
            make_form(db_session, code="AP-X-001", title="重复编号应当被拒",
                      files=1)

        second = _post(client, "/tools/list_pending", {"limit": 10})
        assert second["data"]["sync"]["created"] == 0
        assert second["data"]["sync"]["total"] == 2
        ids1 = {t["approval_code"] for t in first["data"]["tasks"]}
        ids2 = {t["approval_code"] for t in second["data"]["tasks"]}
        assert ids1 == ids2


class TestFullPipeline:
    def test_ac1_to_ac5_offline(self, client: TestClient,
                                db_session: Session, seeded) -> None:
        from app.tools.bootstrap import seed_rules

        seed_rules(db_session)
        _post(client, "/tools/list_pending", {})
        tasks = client.get("/agent/tasks").json()["tasks"]
        buy = next(t for t in tasks if t["approval_code"] == "AP-X-001")

        downloaded = _post(client, "/tools/download_attachment",
                           {"instance_id": buy["instance_id"]})
        assert downloaded["data"]["attachments"][0]["download_status"] == "done"

        parsed = _post(client, "/tools/parse_document", {"document_id": buy["id"]})
        assert parsed["data"]["task_status"] == "reviewing"
        assert parsed["data"]["basic_info"]["amount"]["value"] == 1_860_000.0

        ruled = _post(client, "/tools/run_rules", {"case_id": buy["id"]})
        codes = {h["rule_code"] for h in ruled["data"]["hits"]}
        assert {"PAY_ADVANCE_HIGH", "PAY_CYCLE_LONG", "NO_BREACH",
                "JURISDICTION_RISK"} <= codes
        assert ruled["data"]["overall_risk_level"] == "high"

        label = {"high": "高", "medium": "中", "low": "低"}[
            ruled["data"]["overall_risk_level"]]
        lines = [f"【AI合同审查】总风险等级：{label}", "", "一、命中规则列表"]
        lines += [f"{i}. [{h['risk_level']}] {h['rule_name']}"
                  for i, h in enumerate(ruled["data"]["hits"], 1)]
        saved = _post(client, "/tools/save_result", {
            "case_id": buy["id"],
            "overall_risk_level": ruled["data"]["overall_risk_level"],
            "summary_text": "摘要",
            "focus_points_json": ruled["data"]["focus_points"],
            "comment_text": "\n".join(lines)})
        review_id = saved["data"]["review_id"]

        written = _post(client, "/tools/write_comment",
                        {"instance_id": buy["instance_id"], "review_id": review_id})
        assert written["data"]["write_status"] == "success"
        assert len(seeded) == 1

        again = _post(client, "/tools/write_comment",
                      {"instance_id": buy["instance_id"], "review_id": review_id})
        assert again["data"]["deduped"] is True
        assert len(seeded) == 1

        detail = client.get(f"/agent/tasks/{buy['id']}").json()
        assert detail["task"]["task_status"] == "done"
        assert detail["task"]["write_status"] == "success"


class TestBlockedAndRetry:
    def test_ac6_block_then_retry(self, client: TestClient,
                                  db_session: Session, seeded) -> None:
        _post(client, "/tools/list_pending", {})
        tasks = client.get("/agent/tasks").json()["tasks"]
        broken = next(t for t in tasks if t["approval_code"] == "AP-X-006")

        result = _post(client, "/tools/parse_document", {"document_id": broken["id"]})
        assert result["ok"] is False
        assert result["error"]["code"] == "ATTACHMENT_MISSING"

        detail = client.get(f"/agent/tasks/{broken['id']}").json()
        assert detail["task"]["task_status"] == "blocked"
        assert "ATTACHMENT_MISSING" in detail["task"]["block_reason"]

        retried = client.post(f"/agent/tasks/{broken['id']}/retry")
        assert retried.status_code == 200
        assert retried.json()["resumed_stage"] == "parsing"


class TestGuards:
    def test_comment_missing_marker_auto_completed(
            self, client: TestClient, db_session: Session, seeded) -> None:
        """G7 自动补齐：缺『总风险等级』行由护栏补标准行，不再拒收。"""
        from app.tools.bootstrap import seed_rules

        seed_rules(db_session)
        _post(client, "/tools/list_pending", {})
        tasks = client.get("/agent/tasks").json()["tasks"]
        buy = next(t for t in tasks if t["approval_code"] == "AP-X-001")
        saved = _post(client, "/tools/save_result", {
            "case_id": buy["id"], "overall_risk_level": "low",
            "summary_text": "s", "focus_points_json": [],
            "comment_text": "整体尚可，建议留意交付节点。"})
        assert saved["ok"] is True
        assert saved["data"]["comment_text"].startswith("【AI合同审查】总风险等级：低")
