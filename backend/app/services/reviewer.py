"""解析与审查编排服务（FR-B/FR-D）：解析管线、评论生成、结果保存、幂等回写。"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.core.obs import TOOL_CALLS, get_logger, log_event
from app.models import ApprovalAttachment, ApprovalTask, CommentLog, ContractParse, ReviewResult
from app.services import approval_store
from app.services.parser import ParseError, extract_raw_text, extract_structured
from app.services.state_machine import block_task, transition
from app.services.tool_errors import ToolError

logger = get_logger("reviewer")

_PRIMARY_ORDER = {"docx": 0, "pdf": 1, "md": 2, "txt": 3, "png": 4, "jpg": 4, "jpeg": 4}

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u200b\u200e\u200f]")


def _pick_primary(rows: list[ApprovalAttachment]) -> ApprovalAttachment | None:
    done = [r for r in rows if r.download_status == "done"]
    return min(done, key=lambda r: _PRIMARY_ORDER.get(r.file_type, 99), default=None)


def parse_task(db: Session, task: ApprovalTask) -> ContractParse:
    """解析主附件（docx>pdf>md>txt>图片OCR），落 contract_parses（含 raw_text）。"""
    rows = db.query(ApprovalAttachment).filter_by(task_id=task.id).all()
    primary = _pick_primary(rows)
    if primary is None:
        raise ToolError("PARSE_EMPTY", "没有已下载的可用附件", block_stage="parsing")
    try:
        text, file_type = extract_raw_text(primary.file_path)
        structured = extract_structured(text)
    except ParseError as exc:
        code = "OCR_FAILED" if primary.file_type in ("png", "jpg", "jpeg") else "PARSE_EMPTY"
        raise ToolError(code, str(exc), block_stage="parsing") from exc

    row = db.query(ContractParse).filter_by(task_id=task.id).one_or_none()
    if row is None:
        row = ContractParse(task_id=task.id)
        db.add(row)
    row.raw_text = text[:200_000]
    row.basic_info_json = structured["basic_info"]
    row.clause_info_json = structured["clauses"]
    row.parse_status = "done"
    row.parse_error = None
    db.commit()
    TOOL_CALLS.labels(tool="parse_document", outcome="done").inc()
    return row


def get_upload_dir() -> str:
    from app.core.config import get_settings

    return get_settings().upload_dir


def build_comment_text(summary: dict, parse_row: ContractParse | None) -> str:
    """模板化中文审查意见（LLM 润色在 GPU 联调切片叠加，降级路径即本函数）。"""
    label = summary.get("overall_risk_label",
                         {"low": "低", "medium": "中", "high": "高"}[summary["overall_risk_level"]])
    lines = [f"【AI合同审查】总风险等级：{label}", ""]
    if summary["hits"]:
        lines.append("一、命中规则列表")
        for i, h in enumerate(summary["hits"], 1):
            source = "[AI自由裁量]" if h.get("rule_code") == "AI_DISCRETIONARY" else "[规则]"
            lines.append(f"{i}. {source}({h['risk_level']}) {h['rule_name']}——"
                         f"证据：{h['evidence_text'][:80]}"
                         f"（位置:{h['evidence_position'] or '全文'}）")
        lines.append("")
    title = ""
    if parse_row is not None and isinstance(parse_row.basic_info_json, dict):
        title = (parse_row.basic_info_json.get("contract_title") or {}).get("value") or ""
    lines.append("二、中文摘要")
    lines.append(f"合同「{title or '（未识别标题）'}」经规则库审查，"
                 f"共命中 {len(summary['hits'])} 条风险项，综合风险等级为「{label}」。")
    lines.append("")
    lines.append("三、审批关注点")
    if summary["focus_points"]:
        lines.extend(f"- {pt}" for pt in summary["focus_points"])
    else:
        lines.append("- 未触发规则关注点，请按常规流程审批。")
    return "\n".join(lines)


def sanitize_comment(text: str) -> str:
    cleaned = _CTRL_RE.sub("", text).strip()
    if len(cleaned) > 4000:
        cleaned = cleaned[:3980] + "\n…（超长截断）"
    return cleaned


def validate_comment(text: str) -> None:
    if "总风险等级" not in text:
        raise ToolError("VALIDATION_ERROR", "评论文本缺少『总风险等级』要素")


def save_result(db: Session, task: ApprovalTask, *, overall_risk_level: str,
                summary_text: str, focus_points_json: list,
                comment_text: str) -> ReviewResult:
    """G7 护栏：净化+长度约束；缺『总风险等级』行时自动补齐而非拒收（零步数损耗，
    格式契约对下游永远成立；空文本仍回落模板）。"""
    comment_text = sanitize_comment(comment_text)
    if "总风险等级" not in comment_text:
        label = {"high": "高", "medium": "中", "low": "低"}.get(
            overall_risk_level, "中")
        comment_text = f"【AI合同审查】总风险等级：{label}\n{comment_text}"
        comment_text = sanitize_comment(comment_text)
    row = ReviewResult(task_id=task.id, overall_risk_level=overall_risk_level,
                       summary_text=summary_text,
                       focus_points_json=focus_points_json,
                       comment_text=comment_text)
    db.add(row)
    db.commit()
    TOOL_CALLS.labels(tool="save_result", outcome="done").inc()
    return row


def write_comment(db: Session, task: ApprovalTask, review: ReviewResult) -> dict:
    """幂等回写守卫(G7)：success 即短路；writing→外呼→success/failed；失败进 blocked。"""
    fresh = db.query(ApprovalTask).filter_by(id=task.id).one()
    if fresh.write_status == "success":
        prior = db.query(CommentLog).filter_by(task_id=task.id)\
            .order_by(CommentLog.id.desc()).first()
        return {"write_status": "success", "comment_log_id": prior.id if prior else None,
                "deduped": True}
    if review.task_id != task.id:
        raise ToolError("VALIDATION_ERROR", "review 与任务不匹配")

    fresh.write_status = "writing"
    db.add(CommentLog(task_id=task.id, write_status="writing", write_response_text=None))
    db.commit()
    try:
        resp = approval_store.post_comment(task.instance_id, review.comment_text)
        fresh.write_status = "success"
        db.add(CommentLog(task_id=task.id, write_status="success",
                          write_response_text=str(resp)[:500]))
        db.commit()
        transition(db, fresh, "done")
        TOOL_CALLS.labels(tool="write_comment", outcome="success").inc()
        return {"write_status": "success", "mock_response": resp, "deduped": False}
    except ToolError as exc:
        fresh.write_status = "failed"
        db.add(CommentLog(task_id=task.id, write_status="failed",
                          write_response_text=str(exc)[:500]))
        db.commit()
        TOOL_CALLS.labels(tool="write_comment", outcome="failed").inc()
        block_task(db, fresh, exc.code, str(exc))
        raise


def advance_parse_stage(db: Session, task: ApprovalTask) -> dict:
    """pending/blocked→parsing：下载全部附件并解析；失败按分类学阻塞。"""
    from app.services import fetcher
    from app.services.state_machine import ALLOWED

    if task.task_status == "blocked":
        if not transition(db, task, "parsing"):
            db.refresh(task)
    elif not transition(db, task, "parsing"):
        db.refresh(task)
        if task.task_status != "parsing":
            raise ToolError("INVALID_STATE", f"状态 {task.task_status} 不能进入解析")
    try:
        rows = fetcher.download_all(db, task)
        parse_row = parse_task(db, task)
    except ToolError as exc:
        if not exc.retriable:
            block_task(db, task, exc.code, str(exc))
        raise
    log_event(logger, 20, "stage parse done", task_id=task.id,
              kind=f"attachments={len(rows)}")
    transition(db, task, "reviewing")
    return {"attachments": len(rows),
            "basic_info": parse_row.basic_info_json,
            "clauses": parse_row.clause_info_json}
