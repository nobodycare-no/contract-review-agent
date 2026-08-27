"""V1-S2 业务面测试：多模式上传创建 / 批量送审后台队列收敛 / 队列视图聚合。"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.factory import docx_bytes


def _upload(client: TestClient, *, n_files: int = 1, bundle: bool = False,
            title: str = "集团采购主合同", applicant: str = "王铁柱") -> dict:
    files = [("files", (f"合同V{i}.docx", docx_bytes(), "application/octet-stream"))
             for i in range(1, n_files + 1)]
    r = client.post("/app/forms",
                    data={"applicant": applicant, "title": title,
                          "bundle": str(bundle).lower()},
                    files=files)
    assert r.status_code == 200, r.text
    return r.json()


class TestCreateForms:
    def test_each_mode_makes_one_form_per_file(self, client: TestClient) -> None:
        out = _upload(client, n_files=2)
        assert out["ok"] and len(out["created"]) == 2
        assert {c["attachments"] for c in out["created"]} == {1}

    def test_bundle_mode_single_form_multi_attachment(self, client: TestClient) -> None:
        out = _upload(client, n_files=3, bundle=True, title="采购包（合同+扫描件+清单）")
        assert len(out["created"]) == 1
        assert out["created"][0]["attachments"] == 3

    def test_bad_extension_reported_not_crash(self, client: TestClient) -> None:
        r = client.post("/app/forms", data={"applicant": "x", "bundle": "false"},
                        files=[("files", ("virus.exe", b"MZ", "application/x-msdownload"))])
        body = r.json()
        assert body["ok"] is False
        assert ".exe" in body["errors"][0]["reason"]


class TestBatchReview:
    def test_batch_runs_to_done_with_risk_levels(
            self, client: TestClient, db_session: Session) -> None:
        from app.tools.bootstrap import seed_rules

        seed_rules(db_session)
        a = _upload(client, n_files=1)["created"][0]
        b = _upload(client, n_files=1, title="第二份采购合同")["created"][0]

        r = client.post("/app/batch_review",
                        json={"task_ids": [a["task_id"], b["task_id"]]})
        assert r.status_code == 200 and r.json()["accepted"] == 2

        # BackgroundTasks 在响应后同步执行——轮询至两单完成（限 30 轮）
        for _ in range(30):
            q = client.get("/app/queue").json()
            done = [t for t in q["tasks"]
                    if t["id"] in (a["task_id"], b["task_id"])
                    and t["task_status"] == "done"]
            if len(done) == 2:
                break
            time.sleep(0.1)
        levels = {t["id"]: t["overall_risk_level"]
                  for t in q["tasks"]}
        assert all(levels[i] is not None for i in (a["task_id"], b["task_id"]))
