"""接入层：待办去重拉取(upsert) + 附件下载落盘 + 元数据入库（FR-A）。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.obs import TOOL_CALLS, get_logger, log_event
from app.models import ApprovalAttachment, ApprovalTask
from app.services import mock_client

logger = get_logger("fetcher")

_UNSAFE = re.compile(r"[^\w.\-\u4e00-\u9fff]+")


def _safe_name(name: str) -> str:
    cleaned = _UNSAFE.sub("_", name).strip("._") or "attachment.bin"
    return cleaned[:180]


def sync_pending_approvals(db: Session, limit: int = 20) -> dict:
    """唯一业务标识(approval_code)去重：存在即更新可变字段，绝不重建。"""
    items = mock_client.list_pending(limit=limit)
    created = updated = 0
    for item in items:
        task = db.query(ApprovalTask).filter_by(
            approval_code=item["approval_code"]).one_or_none()
        if task is None:
            task = ApprovalTask(
                approval_code=item["approval_code"],
                approval_title=item["title"],
                applicant_name=item["applicant"],
                instance_id=item["instance_id"])
            db.add(task)
            created += 1
            continue
        changed = False
        for key, value in (("approval_title", item["title"]),
                           ("applicant_name", item["applicant"]),
                           ("instance_id", item["instance_id"])):
            if getattr(task, key) != value:
                setattr(task, key, value)
                changed = True
        updated += 1 if changed else 0
    db.commit()
    TOOL_CALLS.labels(tool="list_pending", outcome="done").inc()
    return {"total": len(items), "created": created, "updated": updated}


def ensure_attachment_rows(db: Session, task: ApprovalTask) -> list[ApprovalAttachment]:
    detail = mock_client.get_detail(task.instance_id)
    existing = {a.attachment_id for a in db.query(ApprovalAttachment)
                .filter_by(task_id=task.id)}
    rows: list[ApprovalAttachment] = []
    for att in detail.get("attachments", []):
        if att["attachment_id"] in existing:
            row = db.query(ApprovalAttachment).filter_by(
                task_id=task.id, attachment_id=att["attachment_id"]).one()
        else:
            row = ApprovalAttachment(task_id=task.id,
                                     attachment_id=att["attachment_id"],
                                     file_name=att["file_name"],
                                     file_type=Path(att["file_name"]).suffix.lstrip(".").lower(),
                                     file_path="", download_status="pending")
            db.add(row)
        rows.append(row)
    db.commit()
    return rows


def download_all(db: Session, task: ApprovalTask) -> list[ApprovalAttachment]:
    """逐附件下载→sha256→落盘；全部成功置 done。任何失败抛 ToolError 由上层决定 blocked。"""
    settings = get_settings()
    rows = ensure_attachment_rows(db, task)
    if not rows:
        from app.services.tool_errors import ToolError

        raise ToolError("ATTACHMENT_MISSING", "审批单没有任何附件",
                        block_stage="parsing")
    base = Path(settings.upload_dir) / str(task.id)
    base.mkdir(parents=True, exist_ok=True)
    try:
        for row in rows:
            if row.download_status == "done" and row.file_path and \
                    Path(row.file_path.replace("/srv/storage", settings.upload_dir)).exists():
                continue
            content, name = mock_client.download_attachment(task.instance_id, row.attachment_id)
            safe = _safe_name(name or f"{row.attachment_id}.bin")
            target = base / safe
            target.write_bytes(content)
            row.file_name = name or safe
            row.file_path = str(target)
            row.download_status = "done"
            log_event(logger, 20, "attachment saved", task_id=task.id,
                      kind=f"{safe}:{hashlib.sha256(content).hexdigest()[:12]}:{len(content)}B")
        db.commit()
    except Exception:
        db.rollback()
        for row in rows:
            if row.download_status != "done":
                row.download_status = "failed"
        db.commit()
        raise
    TOOL_CALLS.labels(tool="download_attachment", outcome="done").inc()
    return rows


def container_path(stored_path: str) -> str:
    """宿主 UPLOAD_DIR 与容器内 /srv/storage 的路径换算。"""
    settings = get_settings()
    return stored_path.replace(settings.upload_dir, "/srv/storage", 1)
