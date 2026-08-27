"""前台业务面（法务/业务人员）：
- 审批单创建（多文件上传；each=每文件一单，bundle=合并为一单多附件）
- 队列视图（含综合风险等级聚合）
- 批量送审（后台顺序队列，HTTP 立即返回进度可轮询）
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, \
    UploadFile
from fastapi.responses import FileResponse
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


@router.get("/files/{task_id}/{attachment_id}")
def view_file(task_id: int, attachment_id: str,
              db: Session = Depends(get_db)):
    """原始合同文件查看（浏览器内联打开；新窗口另存即为下载）。"""
    from fastapi import HTTPException
    from pathlib import Path as _P

    from app.models import ApprovalAttachment

    row = (db.query(ApprovalAttachment)
           .filter_by(task_id=task_id, attachment_id=attachment_id).one_or_none())
    if row is None or not (row.file_path and _P(row.file_path).is_file()):
        raise HTTPException(404, "附件不存在")
    mime = {"docx": ("application/vnd.openxmlformats-officedocument."
                     "wordprocessingml.document", False),
            "pdf": ("application/pdf", True),
            "md": ("text/markdown; charset=utf-8", True),
            "txt": ("text/plain; charset=utf-8", True),
            "png": ("image/png", True), "jpg": ("image/jpeg", True),
            "jpeg": ("image/jpeg", True)}.get(row.file_type.lower(),
                                              ("application/octet-stream", False))
    from urllib.parse import quote

    fname = quote(row.file_name)
    headers = {"Content-Disposition":
               f"inline; filename*=UTF-8''{fname}" if mime[1]
               else f"attachment; filename*=UTF-8''{fname}"}
    return FileResponse(row.file_path, media_type=mime[0], headers=headers)


@router.get("/diag_llm")
def diag_llm() -> dict:
    """发一次最小真实推理请求并原样回报：给用户看得懂的 GPU 链路证据。"""
    import os
    import httpx

    base = os.environ.get("LLM_BASE_URL", "")
    key = os.environ.get("LLM_API_KEY", "")
    out = {"configured": bool(base), "url": base}
    if not base:
        return {**out, "reachable": False, "note": "未配置 LLM_BASE_URL"}
    try:
        r = httpx.post(f"{base.rstrip('/')}/chat/completions",
                       headers={"Authorization": f"Bearer {key}"},
                       json={"model": os.environ.get("LLM_MODEL", "qwen3-8b"),
                             "messages": [{"role": "user",
                                           "content": "只回复两个字：在线"}],
                             "max_tokens": 8, "temperature": 0},
                       timeout=30)
        out.update(status=r.status_code)
        try:
            body = r.json()
            msg = body["choices"][0]["message"]
            out["reply"] = (msg.get("content") or "")[:50]
            out["usage"] = body.get("usage")
        except Exception:  # noqa: BLE001
            out["body_head"] = r.text[:300]
        return out
    except Exception as exc:  # noqa: BLE001 —— 诊断必须返回失败细节而非抛错
        return {**out, "status": None,
                "error": f"{type(exc).__name__}: {exc}"[:300]}


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
