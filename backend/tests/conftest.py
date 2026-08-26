"""pytest 基座：SQLite 内存库 + TestClient 装配（环境变量先于 app 导入设置）。"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator

os.environ.setdefault("MYSQL_URL", "sqlite://")          # health/mysql 探测走内存库
os.environ.setdefault("MOCK_BASE_URL", "http://127.0.0.1:1")  # 不可达端口：mock 组件快速降级
os.environ.setdefault("LLM_BASE_URL", "")                # 未配置 LLM → health 报 not_configured
os.environ.setdefault("ADMIN_TOKEN", "test-admin")
_tmpdir = tempfile.mkdtemp(prefix="cra-test-")
os.environ.setdefault("UPLOAD_DIR", _tmpdir)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db
from app.main import create_app
from app.models import Base


@pytest.fixture()
def engine() -> Iterator[object]:
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db_session(engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    session = factory()
    yield session
    session.close()


@pytest.fixture()
def client(engine) -> Iterator[TestClient]:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)

    def override_get_db() -> Iterator[Session]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    application = create_app()
    application.dependency_overrides[get_db] = override_get_db
    with TestClient(application) as test_client:
        yield test_client
    application.dependency_overrides.clear()
