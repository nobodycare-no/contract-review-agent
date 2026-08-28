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
    """重新处理 = 状态复位(blocked→parsing) + 真正跑引擎。

    历史缺陷：本端点曾只做状态机回拨就返回，任务永远悬在 parsing
    无人处理（统一系统没有后台轮询工人）。现修复为同步跑完整闭环。
    """
    task = db.query(ApprovalTask).filter_by(id=task_id).one_or_none()
    if task is None:
        raise HTTPException(404, "任务不存在")
    try:
        stage = retry_task(db, task)
    except ToolError as exc:
        raise HTTPException(409, str(exc))

    from app.services.engine import run_full_cycle
    from app.services.run_trace import record_tool_trace
    from app.services.state_machine import block_task

    try:
        result = run_full_cycle(db, task, dry_run=False)
    except Exception as exc:  # noqa: BLE001 —— 重试崩溃必须显式落回 blocked
        from app.services.run_trace import record_tool_trace
        from app.services.state_machine import block_task

        db.rollback()
        block_task(db, task, "LLM_RUN_FAILED",
                   f"重试运行失败已安全停机：{exc}"[:300])
        record_tool_trace(db, task, getattr(exc, "trace", None))
        raise HTTPException(502, f"重试失败，任务已转回「需人工处理」：{str(exc)[:200]}") from exc

    record_tool_trace(db, task, result.get("trace"))
    return {"task_id": task.id, "resumed_stage": stage,
            "trace": result.get("trace") or [], **result}


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

    from app.services import engine as engine_module
    from app.services.engine import run_full_cycle
    from app.services.state_machine import transition
    from app.services.tool_errors import ToolError

    if not engine_module.try_acquire(task.id):
        raise HTTPException(409, "该审批单正在审查中——请等当前运行结束（约 30~60 秒）再试")
    try:
        if task.task_status == "done":
            # 再次审查：done 单一键复检——先复位 parsing 再进引擎（车道无关）
            transition(db, task, "parsing")
        elif task.task_status == "blocked":
            # 需人工处理单：复位到 parsing 再进引擎（NO_ATTACHMENTS 会以 409 人话拒绝）
            retry_task(db, task)

        try:
            result = run_full_cycle(db, task, dry_run=bool(req.get("dry_run")))
        except ToolError as exc:
            raise HTTPException(409, exc.code)
        except Exception as exc:  # noqa: BLE001 —— 崩溃必须显式落状态，绝不留孤儿
            from app.services.state_machine import block_task

            db.rollback()   # 工具层可能留下损坏事务——先复位再落 blocked
            block_task(db, task, "LLM_RUN_FAILED", f"运行失败已安全停机：{exc}"[:300])
            # 失败轨迹也留痕（闸门异常携带 .trace）——根因可诊断，而非只剩人话尾巴
            from app.services.run_trace import record_tool_trace

            record_tool_trace(db, task, getattr(exc, "trace", None))
            raise HTTPException(502, f"本次运行失败，任务已转入「需人工处理」：{str(exc)[:200]}") from exc
    finally:
        engine_module.release(task.id)

    view = {"task_id": task.id, **result}
    trace = result.get("trace") or []
    view["trace"] = trace
    from app.services.run_trace import record_tool_trace

    record_tool_trace(db, task, trace)
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
