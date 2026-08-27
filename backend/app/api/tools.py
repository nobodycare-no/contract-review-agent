"""七工具 API（Agent 与 CLI 共用执行器）：统一包络 {ok, data|error}。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApprovalTask, ReviewResult
from app.services import approval_store, fetcher, reviewer
from app.services.rule_engine import run_task_rules
from app.services.tool_errors import ToolError

router = APIRouter(prefix="/tools", tags=["tools"])


def _ok(data: Any) -> dict:
    return {"ok": True, "data": data}


def _err(exc: ToolError) -> dict:
    return {"ok": False,
            "error": {"code": exc.code, "message": str(exc), "retriable": exc.retriable}}


def _task_by(db: Session, *, task_id: int | None = None,
             instance_id: str | None = None) -> ApprovalTask:
    query = db.query(ApprovalTask)
    task = (query.filter_by(id=task_id) if task_id is not None
            else query.filter_by(instance_id=instance_id)).one_or_none()
    if task is None:
        raise ToolError("TASK_NOT_FOUND", f"任务不存在: {task_id or instance_id}")
    return task


def _download_one(db: Session, task: ApprovalTask, attachment_id: str):
    for row in fetcher.ensure_attachment_rows(db, task):
        if row.attachment_id == attachment_id:
            # 复用全量下载的幂等性（done 且文件在盘自动跳过）
            return [r for r in fetcher.download_all(db, task)
                    if r.attachment_id == attachment_id]
    raise ToolError("ATTACHMENT_MISSING", f"未知附件: {attachment_id}", block_stage="parsing")


@router.post("/list_pending")
async def list_pending(req: dict, db: Session = Depends(get_db)) -> dict:
    try:
        stat = fetcher.sync_pending_approvals(db, limit=int(req.get("limit", 20)))
        tasks = db.query(ApprovalTask).order_by(ApprovalTask.id).all()
        return _ok({"sync": stat, "tasks": [{
            "id": t.id, "approval_code": t.approval_code, "title": t.approval_title,
            "applicant": t.applicant_name, "instance_id": t.instance_id,
            "task_status": t.task_status, "write_status": t.write_status,
        } for t in tasks]})
    except ToolError as exc:
        return _err(exc)


@router.post("/get_approval")
async def get_approval(req: dict, db: Session = Depends(get_db)) -> dict:
    try:
        detail = approval_store.get_detail(req["instance_id"])
        task = _task_by(db, instance_id=req["instance_id"])
        return _ok({"detail": detail,
                    "local": {"task_id": task.id, "task_status": task.task_status,
                              "write_status": task.write_status}})
    except ToolError as exc:
        return _err(exc)


@router.post("/download_attachment")
async def download_attachment(req: dict, db: Session = Depends(get_db)) -> dict:
    try:
        task = _task_by(db, instance_id=req["instance_id"])
        rows = (fetcher.download_all(db, task) if req.get("attachment_id") is None
                else _download_one(db, task, req["attachment_id"]))
        return _ok({"attachments": [{
            "attachment_id": r.attachment_id, "file_name": r.file_name,
            "file_type": r.file_type, "download_status": r.download_status}
            for r in rows]})
    except ToolError as exc:
        return _err(exc)


@router.post("/parse_document")
async def parse_document(req: dict, db: Session = Depends(get_db)) -> dict:
    try:
        task = _task_by(db, task_id=req.get("document_id") or req.get("case_id"))
        data = reviewer.advance_parse_stage(db, task)
        return _ok({"task_id": task.id, "task_status": task.task_status,
                    "attachments": data["attachments"],
                    "basic_info": data["basic_info"], "clauses": data["clauses"]})
    except ToolError as exc:
        return _err(exc)


@router.post("/run_rules")
async def run_rules_endpoint(req: dict, db: Session = Depends(get_db)) -> dict:
    try:
        from app.models import ContractParse, ReviewRule

        task = _task_by(db, task_id=req.get("case_id"))
        parse_row = db.query(ContractParse).filter_by(task_id=task.id)\
            .order_by(ContractParse.id.desc()).first()
        if parse_row is None or not parse_row.raw_text:
            raise ToolError("PARSE_EMPTY", "请先调用解析工具", block_stage="parsing")
        rules = db.query(ReviewRule).filter(ReviewRule.rule_status == 1).all()
        summary = run_task_rules(db, task.id, rules, parse_row.raw_text)
        return _ok({"task_id": task.id, **summary})
    except ToolError as exc:
        return _err(exc)


@router.post("/save_result")
async def save_result(req: dict, db: Session = Depends(get_db)) -> dict:
    try:
        task = _task_by(db, task_id=req.get("case_id"))
        row = reviewer.save_result(
            db, task,
            overall_risk_level=req["overall_risk_level"],
            summary_text=req["summary_text"],
            focus_points_json=req.get("focus_points_json", []),
            comment_text=req["comment_text"])
        return _ok({"review_id": row.id, "comment_text": row.comment_text})
    except (ToolError, KeyError) as exc:
        if isinstance(exc, KeyError):
            exc = ToolError("VALIDATION_ERROR", f"缺少字段 {exc}")
        return _err(exc)


@router.post("/write_comment")
async def write_comment(req: dict, db: Session = Depends(get_db)) -> dict:
    try:
        task = _task_by(db, instance_id=req["instance_id"])
        review = db.query(ReviewResult).filter_by(id=req["review_id"]).one_or_none()
        if review is None:
            raise ToolError("VALIDATION_ERROR", f"审查结果不存在: {req['review_id']}")
        return _ok(reviewer.write_comment(db, task, review))
    except ToolError as exc:
        return _err(exc)
