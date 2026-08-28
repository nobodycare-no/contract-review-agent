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
    "done": {"parsing"},                   # 再次审查：已完成单可一键复检（C端刚需）
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


def advance_to(db: Session, task: ApprovalTask, target: str) -> None:
    """沿主干链 pending→parsing→reviewing→done 把任务推进到 target（幂等）。

    动机：ReAct 模型会自主跳步（如上传场景附件已在本地而跳过 download 工具），
    状态迁移若绑死工具序列就会断链——成功却被卡在 pending（真机 153/154 事故）。
    target 不可达（当前为 blocked 等）时不动，由调用方决策。
    """
    chain = ["pending", "parsing", "reviewing", "done"]
    try:
        i = chain.index(task.task_status)
        j = chain.index(target)
    except ValueError:
        return
    if j <= i:
        return
    for step in chain[i + 1 : j + 1]:
        if not transition(db, task, step):
            return


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
    """人工重试：缺附件的单子拒绝空转（不假装开工），其余按 block_stage 回溯。"""
    from app.models import ApprovalAttachment

    if not db.query(ApprovalAttachment).filter_by(task_id=task.id).count():
        raise ToolError("NO_ATTACHMENTS",
                        "该审批单没有任何合同文件——请先在此审批单中补传附件，再重新处理")
    reason = task.block_reason or ""
    stage = "reviewing" if reason.startswith("WRITE_FAILED") else "parsing"
    if not transition(db, task, stage):
        raise ToolError("INVALID_STATE", f"当前状态 {task.task_status} 不允许重试")
    return stage


def recover_interrupted(db: Session) -> int:
    """启动自愈：卡在 parsing/reviewing 且无在途工人的任务 → blocked。

    原因标注只陈述可观察事实（上次运行未完成），不虚构具体成因——
    无论是进程重启打断还是模型自弃闭环，落到用户面前都是同一句实话。
    """
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
                        block_reason="上次运行未完成——任务已冻结待人工恢复"
                                     "（可能因进程重启或运行中断），请点击重新处理")
        if ok:
            fixed += 1
            db.add(TaskLog(task_id=t.id, log_level="warn", log_type="agent",
                           log_content="startup recovery: interrupted -> blocked"))
    db.commit()
    return fixed
