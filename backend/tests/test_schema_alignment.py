"""schema-alignment：ORM 元数据必须与 deploy DDL 表/列集合双向对齐（项目 A 沉淀的防漂移测试）。"""
from __future__ import annotations

import re
from pathlib import Path

from app.models import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
DDL_PATH = REPO_ROOT / "deploy" / "mysql" / "init" / "01_schema.sql"

_CREATE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\)\s*ENGINE=", re.S)


def _ddl_columns() -> dict[str, set[str]]:
    ddl = DDL_PATH.read_text(encoding="utf-8")
    tables: dict[str, set[str]] = {}
    for name, body in _CREATE_RE.findall(ddl):
        cols: set[str] = set()
        for raw_line in body.splitlines():
            line = raw_line.strip().rstrip(",")
            if not line:
                continue
            head = line.split()[0]
            if head.upper() in {"KEY", "UNIQUE", "CONSTRAINT", "PRIMARY", "FOREIGN"}:
                continue
            cols.add(head.strip("`"))
        tables[name] = cols
    return tables


def test_ddl_file_exists() -> None:
    assert DDL_PATH.exists(), f"DDL 不存在: {DDL_PATH}"


def test_every_orm_table_present_in_ddl() -> None:
    ddl = _ddl_columns()
    missing = sorted(set(Base.metadata.tables) - set(ddl))
    assert not missing, f"ORM 有而 DDL 缺失的表: {missing}"


def test_every_ddl_table_present_in_orm() -> None:
    ddl = _ddl_columns()
    extra = sorted(set(ddl) - set(Base.metadata.tables))
    assert not extra, f"DDL 有而 ORM 缺失的表: {extra}"


def test_column_sets_aligned_both_ways() -> None:
    ddl = _ddl_columns()
    problems: list[str] = []
    for name, orm_table in Base.metadata.tables.items():
        orm_cols = {c.name for c in orm_table.columns}
        ddl_cols = ddl.get(name, set())
        only_orm = sorted(orm_cols - ddl_cols)
        only_ddl = sorted(ddl_cols - orm_cols)
        if only_orm:
            problems.append(f"{name}: ORM 多出列 {only_orm}")
        if only_ddl:
            problems.append(f"{name}: DDL 多出列 {only_ddl}")
    assert not problems, "列集合未对齐:\n" + "\n".join(problems)
