"""接入层：待办去重拉取(upsert) + 附件下载落盘 + 元数据入库（FR-A）。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.obs import TOOL_CALLS, get_logger, log_event
from app.models import ApprovalAttachment, ApprovalTask
from app.services import approval_store

logger = get_logger("fetcher")

_UNSAFE = re.compile(r"[^\w.\-\u4e00-\u9fff]+")


def _safe_name(name: str) -> str:
    cleaned = _UNSAFE.sub("_", name).strip("._") or "attachment.bin"
    return cleaned[:180]


def sync_pending_approvals(db: Session, limit: int = 20) -> dict:
    """唯一业务标识(approval_code)去重：存在即更新可变字段，绝不重建。"""
    items = approval_store.list_pending(limit=limit)
    created = updated = 0
    for item in items:
        code = item.get("approval_code")
        title = item.get("approval_title") or item.get("title") or ""
        applicant = item.get("applicant_name") or item.get("applicant") or ""
        task = db.query(ApprovalTask).filter_by(approval_code=code).one_or_none()
        if task is None:
            task = ApprovalTask(
                approval_code=code,
                approval_title=title,
                applicant_name=applicant,
                instance_id=item["instance_id"])
            db.add(task)
            created += 1
            continue
        changed = False
        for key, value in (("approval_title", title),
                           ("applicant_name", applicant),
                           ("instance_id", item["instance_id"])):
            if getattr(task, key) != value:
                setattr(task, key, value)
                changed = True
        updated += 1 if changed else 0
    db.commit()
    TOOL_CALLS.labels(tool="list_pending", outcome="done").inc()
    return {"total": len(items), "created": created, "updated": updated}


def ensure_attachment_rows(db: Session, task: ApprovalTask) -> list[ApprovalAttachment]:
    detail = approval_store.get_detail(task.instance_id)
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
                                     file_path=att.get("file_path", ""),
                                     download_status="pending")
            db.add(row)
        rows.append(row)
    db.commit()
    return rows


def download_all(db: Session, task: ApprovalTask) -> list[ApprovalAttachment]:
    """本地化：源文件已在系统盘，规范化复制到任务目录并置 done（含 SHA 留痕）。"""
    rows = ensure_attachment_rows(db, task)
    if not rows:
        from app.services.tool_errors import ToolError

        raise ToolError("ATTACHMENT_MISSING", "审批单没有任何附件",
                        block_stage="parsing")
    from app.services import approval_store as store

    try:
        store.materialize_into_task_dir(task.id)
        db.expire_all()   # 让复制后的路径/状态回到当前会话视图
        rows = [db.query(ApprovalAttachment)
                .filter_by(task_id=task.id, attachment_id=r.attachment_id).one()
                for r in rows]
        db.commit()
    except Exception:  # noqa: BLE001
        for row in rows:
            if row.download_status != "done":
                row.download_status = "failed"
        db.commit()
        raise
    TOOL_CALLS.labels(tool="download_attachment", outcome="done").inc()
    return rows
