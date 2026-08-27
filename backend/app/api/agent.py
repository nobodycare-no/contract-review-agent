"""Agent/管理面（T5 子集：任务查询/重试/日志）；run 与 runs 断点恢复在 T6 注册。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApprovalAttachment, ApprovalTask, CommentLog, ContractParse, \
    ReviewResult, RuleHit, TaskLog
from app.services.state_machine import retry_task
from app.services.tool_errors import ToolError

router = APIRouter(prefix="/agent", tags=["agent"])


@router.get("/tasks")
def list_tasks(db: Session = Depends(get_db)) -> dict:
    tasks = db.query(ApprovalTask).order_by(ApprovalTask.id.desc()).all()
    return {"tasks": [{
        "id": t.id, "approval_code": t.approval_code, "title": t.approval_title,
        "applicant": t.applicant_name, "instance_id": t.instance_id,
        "task_status": t.task_status, "write_status": t.write_status,
        "block_reason": t.block_reason,
    } for t in tasks]}


@router.get("/tasks/{task_id}")
def task_detail(task_id: int, db: Session = Depends(get_db)) -> dict:
    task = db.query(ApprovalTask).filter_by(id=task_id).one_or_none()
    if task is None:
        raise HTTPException(404, "任务不存在")
    parse_row = db.query(ContractParse).filter_by(task_id=task_id)\
        .order_by(ContractParse.id.desc()).first()
    hits = db.query(RuleHit).filter_by(task_id=task_id).all()
    review = db.query(ReviewResult).filter_by(task_id=task_id)\
        .order_by(ReviewResult.id.desc()).first()
    comments = db.query(CommentLog).filter_by(task_id=task_id)\
        .order_by(CommentLog.id).all()

    from app.models import ReviewRule as _ReviewRule

    rule_map = {r.id: (r.rule_code, r.rule_name, r.risk_level)
                for r in db.query(_ReviewRule).all()}

    def hit_view(h: RuleHit):
        code, name, level = rule_map.get(h.rule_id, ("", f"rule#{h.rule_id}", ""))
        return {"rule_id": h.rule_id, "rule_code": code, "rule_name": name,
                "risk_level": level, "hit_status": h.hit_status,
                "evidence": h.evidence_text[:300], "position": h.evidence_position}

    return {
        "task": {"id": task.id, "approval_code": task.approval_code,
                 "title": task.approval_title, "applicant": task.applicant_name,
                 "instance_id": task.instance_id, "task_status": task.task_status,
                 "write_status": task.write_status, "block_reason": task.block_reason},
        "attachments": [{"attachment_id": a.attachment_id, "file_name": a.file_name,
                         "file_type": a.file_type, "download_status": a.download_status}
                        for a in db.query(ApprovalAttachment).filter_by(task_id=task_id)],
        "parse": None if parse_row is None else {
            "parse_status": parse_row.parse_status,
            "basic_info": parse_row.basic_info_json,
            "clauses": parse_row.clause_info_json,
            "error": parse_row.parse_error},
        "hits": [hit_view(h) for h in hits],
        "review": None if review is None else {
            "review_id": review.id, "overall_risk_level": review.overall_risk_level,
            "summary_text": review.summary_text,
            "focus_points": review.focus_points_json,
            "comment_text": review.comment_text},
        "comment_logs": [{"write_status": c.write_status,
                          "created_at": str(c.created_at)[:19]} for c in comments if c.write_status != "writing"],
    }


@router.post("/tasks/{task_id}/retry")
def retry(task_id: int, db: Session = Depends(get_db)) -> dict:
    task = db.query(ApprovalTask).filter_by(id=task_id).one_or_none()
    if task is None:
        raise HTTPException(404, "任务不存在")
    try:
        stage = retry_task(db, task)
    except ToolError as exc:
        raise HTTPException(409, str(exc))
    return {"task_id": task.id, "resumed_stage": stage}


@router.get("/tasks/{task_id}/logs")
def task_logs(task_id: int, db: Session = Depends(get_db)) -> dict:
    rows = db.query(TaskLog).filter_by(task_id=task_id).order_by(TaskLog.id).all()
    return {"logs": [{"level": r.log_level, "type": r.log_type,
                      "content": r.log_content, "created_at": str(r.created_at)}
                     for r in rows]}


# ---------- RunController 入口（T6） ----------

def _run_view(run) -> dict:
    return {"run_id": run.id, "task_id": run.task_id, "channel": run.channel,
            "status": run.status, "dry_run": bool(run.dry_run),
            "steps_used": run.steps_used, "llm_calls": run.llm_calls,
            "tokens": run.prompt_tokens + run.completion_tokens,
            "wall_ms": run.wall_ms, "fallback_kind": run.fallback_kind,
            "prompt_version": run.prompt_version, "model": run.model_name,
            "error_digest": run.error_digest}


@router.post("/run")
def run(req: dict, db: Session = Depends(get_db)) -> dict:
    from app.services import fetcher
    from app.services.tool_errors import ToolError

    instance_id = req.get("instance_id")
    task = None
    if req.get("task_id") is not None:
        task = db.query(ApprovalTask).filter_by(id=req["task_id"]).one_or_none()
    elif instance_id:
        task = db.query(ApprovalTask).filter_by(instance_id=instance_id).one_or_none()
        if task is None:  # 未入库则先同步一次待办
            fetcher.sync_pending_approvals(db)
            task = db.query(ApprovalTask).filter_by(instance_id=instance_id).one_or_none()
    if task is None:
        raise HTTPException(404, f"找不到任务: {req}")

    from app.services.engine import run_full_cycle
    result = run_full_cycle(db, task, dry_run=bool(req.get("dry_run")))
    run_view_extra=result
    except ToolError as exc:
        raise HTTPException(409, exc.code)

    view = _run_view(run)
    view["trace"] = controller.ctx.trace
    return view


@router.post("/runs/{run_id}/resume")
def resume_run(run_id: int, db: Session = Depends(get_db)) -> dict:
    from app.services.tool_errors import ToolError

    run = db.query(AgentRun).filter_by(id=run_id).one_or_none()
    if run is None:
        raise HTTPException(404, "run 不存在")
    task = db.query(ApprovalTask).filter_by(id=run.task_id).one()
    try:
        run = RunController(db, task).resume(run_id)
    except ToolError as exc:
        raise HTTPException(409, exc.code)
    return _run_view(run)
