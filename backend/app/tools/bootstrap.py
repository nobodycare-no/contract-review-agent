"""规则库种子引导：python -m app.tools.bootstrap [--create-all]

--create-all 仅用于本地快速起库；生产以 deploy/mysql/init DDL 为权威。
"""
from __future__ import annotations

import sys

from app.core.obs import get_logger, setup_logging
from app.db import SessionLocal
from app.models.base import Base
from app.services.rule_seed import SEED_RULES

logger = get_logger("bootstrap")


def seed_rules(db) -> tuple[int, int]:
    """按 rule_code 幂等灌入规则，返回 (新建数, 更新数)。"""
    from app.models import ReviewRule

    created = updated = 0
    for item in SEED_RULES:
        obj = db.query(ReviewRule).filter_by(rule_code=item["rule_code"]).one_or_none()
        if obj is None:
            db.add(ReviewRule(**item))
            created += 1
            continue
        changed = False
        for key, value in item.items():
            if getattr(obj, key) != value:
                setattr(obj, key, value)
                changed = True
        if changed:
            updated += 1
    db.commit()
    return created, updated


def main() -> int:
    setup_logging()
    if "--create-all" in sys.argv:
        from app.db import engine

        Base.metadata.create_all(engine)
        logger.info("create_all done")
    with SessionLocal() as db:
        created, updated = seed_rules(db)
    logger.info("rules seeded", extra={"kind": f"created={created},updated={updated}"})
    print(f"规则种子完成：新建 {created} 条，更新 {updated} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
