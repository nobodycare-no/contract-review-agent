"""任务状态机（规范 §2.4.4）：CAS 迁移 + blocked/retry 语义。"""
from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.obs import BLOCKED, get_logger, log_event
from app.models import ApprovalTask, TaskLog
from app.services.tool_errors import ToolError, to_blocked_stage

logger = get_logger("state_machine")

ALLOWED: dict[str, set[str]] = {
    "pending": {"parsing", "blocked"},
    "parsing": {"reviewing", "blocked"},
    "reviewing": {"done", "blocked"},
    "blocked": {"parsing", "reviewing"},   # retry 回溯
    "done": set(),
}


def transition(db: Session, task: ApprovalTask, to_status: str, *,
               block_reason: str | None = None) -> bool:
    """CAS 状态迁移：受影响行数=0 即竞争失败（返回 False，由调用方重读）。"""
    if to_status not in ALLOWED.get(task.task_status, set()):
        return False
    result = db.execute(
        update(ApprovalTask)
        .where(ApprovalTask.id == task.id,
               ApprovalTask.task_status == task.task_status)
        .values(task_status=to_status,
                block_reason=block_reason if to_status == "blocked" else None))
    db.commit()
    if result.rowcount != 1:
        return False
    task.task_status = to_status
    log_event(logger, 20, "task transition", task_id=task.id,
              kind=f"{to_status}" + (f":{block_reason[:80]}" if block_reason else ""))
    return True


def block_task(db: Session, task: ApprovalTask, code: str, message: str) -> None:
    """统一阻塞入口：写状态/原因/任务日志/指标。"""
    stage = to_blocked_stage(code)
    if not transition(db, task, "blocked", block_reason=f"{code}: {message}"[:500]):
        db.refresh(task)
        return
    db.add(TaskLog(task_id=task.id, log_level="error", log_type="agent",
                   log_content=f"BLOCKED {code}: {message}"[:1000]))
    db.commit()
    BLOCKED.labels(reason=code).inc()


def retry_task(db: Session, task: ApprovalTask) -> str:
    """人工重试：按 block_stage 回溯 parsing 或 reviewing。"""
    reason = task.block_reason or ""
    stage = "reviewing" if reason.startswith("WRITE_FAILED") else "parsing"
    if not transition(db, task, stage):
        raise ToolError("INVALID_STATE", f"当前状态 {task.task_status} 不允许重试")
    return stage


def recover_interrupted(db: Session) -> int:
    """启动自愈：进程曾中断导致卡在 parsing/reviewing 的孤儿任务 → blocked（人话原因）。"""
    from app.models import AgentRun

    running_ids = {r.task_id for r in db.query(AgentRun)
                   .filter_by(status="running").all()}
    fixed = 0
    for t in (db.query(ApprovalTask)
              .filter(ApprovalTask.task_status.in_(["parsing", "reviewing"]))
              .all()):
        if t.id in running_ids:
            continue
        ok = transition(db, t, "blocked",
                        block_reason="系统维护中断，处理未完成——请点击重新处理")
        if ok:
            fixed += 1
            db.add(TaskLog(task_id=t.id, log_level="warn", log_type="agent",
                           log_content="startup recovery: interrupted -> blocked"))
    db.commit()
    return fixed
