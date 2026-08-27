"""前台业务面（法务/业务人员）：
- 审批单创建（多文件上传；each=每文件一单，bundle=合并为一单多附件）
- 队列视图（含综合风险等级聚合）
- 批量送审（后台顺序队列，HTTP 立即返回进度可轮询）
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, \
    UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApprovalTask, ReviewResult
from app.services import approval_store

router = APIRouter(prefix="/app", tags=["portal"])

_ALLOWED_EXT = {"docx", "pdf", "md", "txt", "png", "jpg", "jpeg"}
_MAX_FILE_MB = 20


def _risk_levels(db: Session, task_ids: list[int]) -> dict[int, str]:
    rows = (db.query(ReviewResult.task_id,
                     func.max(ReviewResult.id))
            .filter(ReviewResult.task_id.in_(task_ids))
            .group_by(ReviewResult.task_id).all())
    latest_ids = [rid for _, rid in rows]
    out: dict[int, str] = {}
    if latest_ids:
        for r in db.query(ReviewResult).filter(ReviewResult.id.in_(latest_ids)):
            out[r.task_id] = r.overall_risk_level
    return out


@router.post("/forms")
async def create_forms(
    title: str = Form(""),
    applicant: str = Form(...),
    bundle: bool = Form(False),
    files: list[UploadFile] = File(default_factory=list),
    db: Session = Depends(get_db),
) -> dict:
    """创建审批单。bundle=false 时每个附件各成一张单（标题取文件名主部）。"""
    created: list[dict] = []
    errors: list[dict] = []

    blobs: list[tuple[str, bytes]] = []
    for f in files or []:
        ext = f.filename.rsplit(".", 1)[-1].lower() if f.filename and "." in f.filename else ""
        blob = await f.read()
        if not blob:
            errors.append({"file": f.filename, "reason": "空文件"})
            continue
        if ext not in _ALLOWED_EXT:
            errors.append({"file": f.filename, "reason": f"不支持的类型 .{ext}"})
            continue
        if len(blob) > _MAX_FILE_MB * 1024 * 1024:
            errors.append({"file": f.filename, "reason": f"超过{_MAX_FILE_MB}MB"})
            continue
        blobs.append((f.filename or f"attachment.{ext}", blob))

    def _mk(t: str, pairs):
        task = approval_store.create_form(title=t, applicant=applicant,
                                          sources=pairs)
        created.append({"task_id": task.id,
                        "approval_code": task.approval_code,
                        "instance_id": task.instance_id,
                        "title": t, "attachments": len(pairs)})

    try:
        if bundle and blobs:
            _mk(title.strip() or "合并审查合同", blobs)
        elif bundle and not blobs:
            errors.append({"file": "-", "reason": "打包模式缺少文件"})
        else:
            for name, blob in blobs:
                stem = name.rsplit(".", 1)[0]
                _mk(stem[:60], [(name, blob)])
    except Exception as exc:  # noqa: BLE001 —— 编号冲突等收敛为错误行
        errors.append({"file": title or "-", "reason": str(exc)[:160]})

    return {"ok": bool(created),
            "created": created, "errors": errors}


@router.get("/queue")
def queue(status: str | None = Query(None), limit: int = Query(50, le=200),
          db: Session = Depends(get_db)) -> dict:
    q = db.query(ApprovalTask)
    if status:
        q = q.filter(ApprovalTask.task_status == status)
    tasks = q.order_by(ApprovalTask.id.desc()).limit(limit).all()
    risks = _risk_levels(db, [t.id for t in tasks])
    return {"tasks": [{
        "id": t.id, "approval_code": t.approval_code,
        "title": t.approval_title, "applicant": t.applicant_name,
        "instance_id": t.instance_id,
        "task_status": t.task_status, "write_status": t.write_status,
        "overall_risk_level": risks.get(t.id),
        "block_reason": t.block_reason,
    } for t in tasks],
        "counts": dict(db.query(ApprovalTask.task_status,
                                func.count(ApprovalTask.id))
                       .group_by(ApprovalTask.task_status).all())}


def _run_batch(ids: list[int]) -> None:
    """批量送审工人：顺序执行，任何异常都被吞掉并在该任务的运行记录/状态中留痕。"""
    from app.db import SessionLocal
    from app.services.agent_loop import RunController

    for tid in ids:
        s = SessionLocal()
        try:
            task = s.query(ApprovalTask).filter_by(id=tid).one_or_none()
            if task is None:
                continue
            RunController(s, task).start()
        except Exception:  # noqa: BLE001 —— 批量工人绝不向上抛
            continue
        finally:
            s.close()


@router.post("/batch_review")
def batch_review(payload: dict,
                 background_tasks: "BackgroundTasks",
                 db: Session = Depends(get_db)) -> dict:
    ids = [int(i) for i in payload.get("task_ids", [])]
    if not ids:
        from fastapi import HTTPException

        raise HTTPException(422, "task_ids 为空")
    background_tasks.add_task(_run_batch, ids)
    return {"accepted": len(ids), "note": "后台顺序执行中，请轮询 /app/queue"}
