"""七工具注册表：OpenAI JSON Schema（规范 §2.4.10 签名逐一对应）+ 统一执行器。

Agent 循环、确定性通道、CLI 三方共用同一 dispatch——单一事实源。
工具结果回填前截断至 MAX_RESULT_CHARS。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.models import ApprovalTask
from app.services import fetcher, reviewer
from app.services.rule_engine import run_task_rules
from app.services.tool_errors import ToolError

MAX_RESULT_CHARS = 2000


def _fn(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required}}}


TOOLS_SCHEMA: list[dict] = [
    _fn("list_pending_contract_approvals", "拉取待处理审批单列表",
        {"limit": {"type": "integer"}}, []),
    _fn("get_contract_approval", "查询单个审批单详情",
        {"instance_id": {"type": "string"}}, ["instance_id"]),
    _fn("download_contract_attachment", "下载合同附件并返回本地路径与校验信息",
        {"instance_id": {"type": "string"}, "attachment_id": {"type": "string"}},
        ["instance_id"]),
    _fn("parse_contract_document", "解析合同文档返回结构化字段",
        {"document_id": {"type": "integer"}}, ["document_id"]),
    _fn("run_contract_rules", "执行规则审查返回命中结果和风险结论",
        {"case_id": {"type": "integer"}}, ["case_id"]),
    _fn("save_review_result", "保存审查结果(总风险等级/摘要/关注点/评论全文)",
        {"case_id": {"type": "integer"}, "overall_risk_level": {"type": "string",
         "enum": ["high", "medium", "low"]},
         "summary_text": {"type": "string"},
         "focus_points_json": {"type": "array", "items": {"type": "string"}},
         "comment_text": {"type": "string"}},
        ["case_id", "overall_risk_level", "summary_text", "focus_points_json",
         "comment_text"]),
    _fn("write_approval_comment", "将审查意见写回审批评论区",
        {"instance_id": {"type": "string"}, "review_id": {"type": "integer"}},
        ["instance_id", "review_id"]),
]


@dataclass
class RunContext:
    db: Session
    task: ApprovalTask
    dry_run: bool = False
    rules_summary: dict | None = None
    review_id: int | None = None
    written: bool = False
    trace: list[dict] = field(default_factory=list)


def _clip(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + "…(截断)"


def _task_by(db: Session, task_id: int) -> ApprovalTask:
    task = db.query(ApprovalTask).filter_by(id=task_id).one_or_none()
    if task is None:
        raise ToolError("TASK_NOT_FOUND", f"任务不存在: {task_id}")
    return task


def execute_tool(ctx: RunContext, name: str, args: dict) -> str:
    """执行单个工具，返回回填给模型的结果字符串；异常收敛为错误文本。"""
    db, task = ctx.db, ctx.task
    try:
        if name == "list_pending_contract_approvals":
            result = fetcher.sync_pending_approvals(db, limit=int(args.get("limit", 20)))
        elif name == "get_contract_approval":
            from app.services import mock_client

            detail = mock_client.get_detail(task.instance_id)
            result = {"approval_code": detail.get("approval_code"),
                      "title": detail.get("approval_title"),
                      "form_data": detail.get("form_data"),
                      "attachments": detail.get("attachments"),
                      "local_task_status": task.task_status}
        elif name == "download_contract_attachment":
            rows = fetcher.download_all(db, task)
            if task.task_status == "pending":
                from app.services.state_machine import transition

                transition(db, task, "parsing")
            result = {"attachments": [{
                "file_name": r.file_name, "file_type": r.file_type,
                "local_path": r.file_path, "sha256_head": r.download_status}
                for r in rows]}
        elif name == "parse_contract_document":
            parse_row = reviewer.parse_task(db, task)
            if task.task_status == "parsing":
                from app.services.state_machine import transition

                transition(db, task, "reviewing")
            result = {"basic_info": parse_row.basic_info_json,
                      "clauses": {k: v["status"] for k, v in
                                  (parse_row.clause_info_json or {}).items()}}
        elif name == "run_contract_rules":
            from app.models import ContractParse, ReviewRule

            parse_row = db.query(ContractParse).filter_by(task_id=task.id).one_or_none()
            if parse_row is None or not parse_row.raw_text:
                raise ToolError("PARSE_EMPTY", "请先解析合同", block_stage="parsing")
            rules = db.query(ReviewRule).filter(ReviewRule.rule_status == 1).all()
            summary = run_task_rules(db, task.id, rules, parse_row.raw_text)
            from app.services.ai_reviewer import augment

            summary = augment(summary, parse_row.raw_text)   # ADR-B10 增量风险层
            ctx.rules_summary = summary
            result = {"overall_risk_level": summary["overall_risk_level"],
                      "hits": summary["hits"], "focus_points": summary["focus_points"]}
        elif name == "save_review_result":
            summary = ctx.rules_summary or {}
            row = reviewer.save_result(
                db, task,
                overall_risk_level=args.get("overall_risk_level") or
                summary.get("overall_risk_level", "low"),
                summary_text=args.get("summary_text") or
                f"命中 {len(summary.get('hits', []))} 条规则。",
                focus_points_json=args.get("focus_points_json") or
                summary.get("focus_points", []),
                comment_text=args.get("comment_text") or
                reviewer.build_comment_text(summary, None))
            ctx.review_id = row.id
            result = {"review_id": row.id}
        elif name == "write_approval_comment":
            if ctx.dry_run:
                ctx.trace.append({"tool": name, "outcome": "dry_run_skip"})
                return _clip({"write_status": "dry_run_skipped",
                              "note": "dry-run 模式：跳过评论外呼"})
            review_id = args.get("review_id") or ctx.review_id
            if not review_id:
                raise ToolError("VALIDATION_ERROR", "尚无已保存的审查结果")
            from app.models import ReviewResult

            review = db.query(ReviewResult).filter_by(id=review_id).one()
            outcome = reviewer.write_comment(db, task, review)
            ctx.written = outcome.get("write_status") == "success"
            result = {"write_status": outcome.get("write_status"),
                      "deduped": outcome.get("deduped", False)}
        else:
            raise ToolError("UNKNOWN_TOOL", f"未注册工具: {name}")
        ctx.trace.append({"tool": name, "outcome": "ok"})
        return _clip(result)
    except ToolError as exc:
        ctx.trace.append({"tool": name, "outcome": exc.code})
        return _clip({"error_code": exc.code, "message": str(exc),
                      "retriable": exc.retriable})
    except Exception as exc:  # noqa: BLE001 —— 工具异常回填自纠，不终止循环
        ctx.trace.append({"tool": name, "outcome": "EXC"})
        return _clip({"error_code": "TOOL_EXCEPTION", "message": str(exc)[:300]})


# ---------- 确定性直调路径（LLM 全不可用时的 ADR-B5 兜底） ----------

DETERMINISTIC_PLAN: list[tuple[str, dict]] = [
    ("get_contract_approval", {}),
    ("download_contract_attachment", {}),
    ("parse_contract_document", {}),
    ("run_contract_rules", {}),
    ("save_review_result", {}),
    ("write_approval_comment", {}),
]


def run_deterministic(ctx: RunContext) -> list[str]:
    """顺序直调七工具子集完成闭环；返回各步结果摘要。"""
    outputs: list[str] = []
    for name, args in DETERMINISTIC_PLAN:
        outputs.append(execute_tool(ctx, name, dict(args)))
    return outputs


# JSON 协议行解析（json 通道）
def parse_protocol_line(content: str) -> tuple[str | None, dict]:
    """从模型文本中提取 {'tool':..,'args':..} 或 {'final':..}；宽松取首个平衡块。"""
    content = content.strip()
    start = content.find("{")
    while start >= 0:
        depth = 0
        for idx in range(start, len(content)):
            if content[idx] == "{":
                depth += 1
            elif content[idx] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(content[start:idx + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict):
                        if "final" in obj:
                            return None, obj
                        tool = obj.get("tool") or obj.get("name")
                        if tool:
                            return str(tool), obj.get("args") or {}
                    break
        start = content.find("{", start + 1)
    return None, {}


def build_system_prompt(channel: str) -> str:
    from app.core.prompts import render

    json_protocol = "" if channel == "native" else (
        "\n输出约定(JSON协议)：每轮仅输出一行 JSON："
        '{"tool":"工具名","args":{参数}} 调用工具；或 {"final":"简短结论"} 结束。')
    return render("agent_system", json_protocol=json_protocol)


def user_briefing(task: ApprovalTask) -> str:
    return (f"请处理审批单：instance_id={task.instance_id}，"
            f"approval_code={task.approval_code}，标题「{task.approval_title}」。"
            "按闭环流程调用工具完成下载→解析→规则审查→保存结果→回写评论。")


def tools_for_channel(channel: str) -> list[dict] | None:
    return TOOLS_SCHEMA if channel == "native" else None
