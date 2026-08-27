"""pytest 基座（V1 合并态）：全部层共享同一文件型 SQLite 测试库，
approval_store/FastAPI依赖注入/直接Session 三方看到同一份数据。
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator

_tmpdir = tempfile.mkdtemp(prefix="cra-test-")
os.environ.setdefault("MYSQL_URL", f"sqlite:///{_tmpdir}/cra_test.sqlite3")
os.environ.setdefault("LLM_BASE_URL", "")          # 未配置 LLM → health 报 not_configured
os.environ.setdefault("ADMIN_TOKEN", "test-admin")
os.environ.setdefault("UPLOAD_DIR", os.path.join(_tmpdir, "attachments"))
os.environ.setdefault("AGENT_ENGINE", "legacy")   # 单测默认 legacy；LC 真机路径见联调脚本

import pytest                                        # noqa: E402
from fastapi.testclient import TestClient            # noqa: E402

from app.db import engine, SessionLocal              # noqa: E402
from app.main import create_app                      # noqa: E402
from app.models import Base                          # noqa: E402


@pytest.fixture()
def _fresh_schema() -> Iterator[None]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture()
def db_session(_fresh_schema) -> Iterator[object]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(_fresh_schema) -> Iterator[TestClient]:
    application = create_app()
    with TestClient(application) as test_client:
        yield test_client
