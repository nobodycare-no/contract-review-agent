"""七工具注册表：OpenAI JSON Schema（规范 §2.4.10 签名逐一对应）+ 统一执行器。

Agent 循环、确定性通道、CLI 三方共用同一 dispatch——单一事实源。
工具结果回填前截断至 MAX_RESULT_CHARS。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.core.obs import log_event
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
    # 参数只描述 dispatch 真实消费的内容——上下文对象(ctx.task)携带的信息不重复声明，
    # 否则模型被迫编造 case_id/instance_id 之类永不被读取的假参数（schema 债）。
    _fn("list_pending_contract_approvals", "拉取待处理审批单列表",
        {"limit": {"type": "integer"}}, []),
    _fn("get_contract_approval", "查看当前审批单详情：基本信息/表单字段/附件清单",
        {}, []),
    _fn("download_contract_attachment", "下载当前审批单的全部合同附件到本地存储",
        {}, []),
    _fn("parse_contract_document", "解析当前审批单的合同正文，产出结构化字段与条款",
        {}, []),
    _fn("run_contract_rules", "对当前审批单执行规则库初筛（参考线索，非结论）",
        {}, []),
    _fn("save_review_result", "保存你亲笔撰写的审查结果",
        {"overall_risk_level": {"type": "string",
         "enum": ["high", "medium", "low"]},
         "summary_text": {"type": "string"},
         "focus_points_json": {"type": "array", "items": {"type": "string"}},
         "comment_text": {"type": "string"}},
        ["overall_risk_level", "comment_text"]),
    _fn("write_approval_comment", "将已保存的审查意见写回审批单评论区（闭环终点，必调）",
        {"review_id": {"type": "integer"}}, []),
    _fn("submit_basic_info", "用合同原文核对解析结果后，修正基本信息"
        "（甲方/乙方/金额/日期等）——AI 以原文为准，解析器只是初稿",
        {"fields": {"type": "object",
                    "description": "字段名→修正值，如 {\"party_a\":\"XX公司\",\"amount\":\"1,860,000\"}"}},
        ["fields"]),
    _fn("list_review_rules", "浏览公司规则库清单（AI 的参考线索工具：规则不是结论，"
        "须回到合同原文独立核实）",
        {"keyword": {"type": "string"}}, []),
    _fn("search_contract_text", "在已解析的合同原文中按关键词定位条款原文（上下文有限时优先检索而非通读）",
        {"keyword": {"type": "string"}}, ["keyword"]),
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
            from app.services import approval_store   # 本地审批域（mock 已物理删除）

            detail = approval_store.get_detail(task.instance_id)
            result = {"approval_code": detail.get("approval_code"),
                      "title": detail.get("title"),
                      "applicant": detail.get("applicant"),
                      "form_data": detail.get("form_data"),
                      "status": detail.get("status"),
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
            # 按意图推进：模型可能跳过 download（上传场景附件已在本地），
            # 此时任务仍是 pending——沿主干链推进到 reviewing，不绑工具顺序
            from app.services.state_machine import advance_to

            advance_to(db, task, "reviewing")
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

            summary = augment(summary, parse_row.raw_text,
                              task=task, db=db)      # ADR-B10 增量风险层(含落库)
            ctx.rules_summary = summary
            result = {"overall_risk_level": summary["overall_risk_level"],
                      "hits": summary["hits"], "focus_points": summary["focus_points"]}
        elif name == "save_review_result":
            comment_text = args.get("comment_text")
            if not comment_text or not str(comment_text).strip():
                # 意见完全来自 AI：拒绝确定性文案兜底（用户 2026-08-28 产品规则）
                raise ToolError(
                    "VALIDATION_ERROR",
                    "审查意见必须由AI亲笔撰写——请综合合同原文与参考线索完成意见后，"
                    "以 comment_text 传入本工具保存")
            # 风险枚举归一：模型常传中文「高/中/低」，落库前统一为 high/medium/low
            _RISK_MAP = {"高": "high", "中": "medium", "低": "low",
                         "high": "high", "medium": "medium", "low": "low"}
            raw_level = str(args.get("overall_risk_level")
                            or (ctx.rules_summary or {}).get("overall_risk_level", "")
                            or "").strip()
            level = _RISK_MAP.get(raw_level.lower(), None)
            if level is None:
                raise ToolError("VALIDATION_ERROR",
                                f"overall_risk_level 必须是 高|中|低 或 high|medium|low，"
                                f"收到: {raw_level!r}")
            summary = ctx.rules_summary or {}
            row = reviewer.save_result(
                db, task,
                overall_risk_level=level,
                summary_text=args.get("summary_text") or
                f"命中 {len(summary.get('hits', []))} 条规则。",
                focus_points_json=args.get("focus_points_json") or
                summary.get("focus_points", []),
                comment_text=comment_text)
            ctx.review_id = row.id
            result = {"review_id": row.id}
        elif name == "submit_basic_info":
            from app.models import ContractParse

            fields = args.get("fields") or {}
            if not isinstance(fields, dict) or not fields:
                raise ToolError("VALIDATION_ERROR", "fields 不能为空")
            parse_row = (db.query(ContractParse).filter_by(task_id=task.id)
                         .order_by(ContractParse.id.desc()).first())
            if parse_row is None:
                raise ToolError("PARSE_EMPTY", "请先解析合同，再修正基本信息")
            _KNOWN = {"contract_title", "contract_no", "party_a", "party_b",
                      "amount", "currency", "effective_date", "expire_date"}
            info = dict(parse_row.basic_info_json or {})
            updated = []
            for k, v in fields.items():
                if k not in _KNOWN or v is None or not str(v).strip():
                    continue
                info[k] = {"value": str(v).strip(), "pos": None,
                           "status": "ai_verified"}
                updated.append(k)
            if not updated:
                raise ToolError("VALIDATION_ERROR",
                                f"没有可识别的字段名，可用: {sorted(_KNOWN)}")
            parse_row.basic_info_json = info
            db.commit()
            result = {"updated": sorted(updated)}
        elif name == "write_approval_comment":
            if ctx.dry_run:
                ctx.trace.append({"tool": name, "outcome": "dry_run_skip"})
                return _clip({"write_status": "dry_run_skipped",
                              "note": "dry-run 模式：跳过评论外呼"})
            review_id = args.get("review_id") or ctx.review_id
            if not review_id:
                raise ToolError("VALIDATION_ERROR", "尚无已保存的审查结果")
            from app.models import ReviewResult

            review = db.query(ReviewResult).filter_by(id=review_id).one_or_none()
            if review is None and ctx.review_id:
                # 韧性对齐：引用不存在的历史 id（如录制轨迹/跨库迁移）时，
                # 回退到本运行最近一次成功保存的结果——"写最新结论"的意图不变
                from app.core.obs import get_logger as _gl

                log_event(_gl("tools"), 30, "review_id aligned to latest",
                          err=str(review_id))
                review_id = ctx.review_id
                review = db.query(ReviewResult).filter_by(id=review_id).one()
            elif review is None:
                raise ToolError("VALIDATION_ERROR", f"审查结果不存在: {review_id}")
            outcome = reviewer.write_comment(db, task, review)
            ctx.written = outcome.get("write_status") == "success"
            result = {"write_status": outcome.get("write_status"),
                      "deduped": outcome.get("deduped", False)}
        elif name == "list_review_rules":
            from app.models import ReviewRule

            kw = (args.get("keyword") or "").strip()
            rules = db.query(ReviewRule).filter(ReviewRule.rule_status == 1).all()
            items = [{"code": r.rule_code, "name": r.rule_name,
                      "risk_level": r.risk_level, "match_mode": r.match_mode,
                      "match_text": r.match_text[:120],
                      "suggestion": r.suggestion_text}
                     for r in rules
                     if not kw or kw in r.rule_name or kw in r.suggestion_text]
            result = {"note": "规则库仅作参考线索——最终判断必须基于合同原文独立作出",
                      "count": len(items), "rules": items[:20]}
        elif name == "search_contract_text":
            from app.models import ContractParse

            kw = str(args.get("keyword") or "").strip()
            if not kw:
                raise ToolError("VALIDATION_ERROR", "keyword 不能为空")
            parse_row = (db.query(ContractParse).filter_by(task_id=task.id)
                         .order_by(ContractParse.id.desc()).first())
            if parse_row is None or not parse_row.raw_text:
                raise ToolError("PARSE_EMPTY", "请先解析合同，再检索条款原文")
            low, kl = parse_row.raw_text.lower(), kw.lower()
            matches, start = [], 0
            while len(matches) < 8:
                idx = low.find(kl, start)
                if idx < 0:
                    break
                matches.append({
                    "position": f"offset:{idx}",
                    "snippet": parse_row.raw_text[max(0, idx - 40): idx + len(kw) + 120]})
                start = idx + len(kw)
            result = {"keyword": kw, "count": len(matches), "matches": matches}
        else:
            raise ToolError("UNKNOWN_TOOL", f"未注册工具: {name}")
        ctx.trace.append({"tool": name, "outcome": "ok"})
        return _clip(result)
    except ToolError as exc:
        ctx.trace.append({"tool": name, "outcome": exc.code})
        return _clip({"error_code": exc.code, "message": str(exc),
                      "retriable": exc.retriable})
    except Exception as exc:  # noqa: BLE001 —— 工具异常回填自纠，不终止循环
        # 轨迹必须带原因：只记 EXC 等于让故障永远匿名（真机 2026-08-28 教训）
        ctx.trace.append({"tool": name, "outcome": f"EXC:{str(exc)[:120]}"})
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
