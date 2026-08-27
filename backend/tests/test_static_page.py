"""T7 静态工作台挂载验证：同源首页可达且不遮蔽 API。"""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_index_served_and_api_unmasked(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "合同智能审查助手" in resp.text

    health = client.get("/health")
    assert health.status_code == 200 and "components" in health.json()

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
