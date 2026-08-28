"""工具轨迹落盘：引擎 trace → TaskLog(type=tool)。

留痕时间线的数据源——任何一次运行（单发/批量）的工具调用与结果码
都必须可在详情页追溯，绝不静默丢弃。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import ApprovalTask, TaskLog


def record_tool_trace(db: Session, task: ApprovalTask, trace) -> None:
    for item in trace or []:
        db.add(TaskLog(
            task_id=task.id, log_level="info", log_type="tool",
            log_content=f"{item.get('tool', '?')} → {item.get('outcome', '?')}"[:1000]))
    if trace:
        db.commit()
