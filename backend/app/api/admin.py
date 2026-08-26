"""管理面（X-Admin-Token 常量时比较）：规则启停 / 演示重置 / 日志别名。"""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db import get_db
from app.models import ReviewRule
from app.services import mock_client
from app.services.tool_errors import ToolError

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(x_admin_token: str = Header(default="")) -> None:
    if not hmac.compare_digest(x_admin_token, get_settings().admin_token):
        raise HTTPException(401, "invalid admin token")


@router.get("/rules", dependencies=[Depends(require_admin)])
def list_rules(db: Session = Depends(get_db)) -> dict:
    rules = db.query(ReviewRule).order_by(ReviewRule.id).all()
    return {"rules": [{"id": r.id, "rule_code": r.rule_code, "rule_name": r.rule_name,
                       "risk_level": r.risk_level, "rule_status": r.rule_status,
                       "match_mode": r.match_mode} for r in rules]}


@router.put("/rules/{rule_code}", dependencies=[Depends(require_admin)])
def update_rule(rule_code: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    rule = db.query(ReviewRule).filter_by(rule_code=rule_code).one_or_none()
    if rule is None:
        raise HTTPException(404, "规则不存在")
    if "rule_status" in payload:
        rule.rule_status = 1 if payload["rule_status"] else 0
    for field in ("rule_name", "risk_level", "match_mode", "match_text", "suggestion_text"):
        if field in payload:
            setattr(rule, field, payload[field])
    db.commit()
    return {"rule_code": rule.rule_code, "rule_status": rule.rule_status}


@router.post("/reset-demo", dependencies=[Depends(require_admin)])
def reset_demo() -> dict:
    try:
        mock_client.reset_mock()
        return {"reset": True}
    except ToolError as exc:
        raise HTTPException(502, str(exc))
