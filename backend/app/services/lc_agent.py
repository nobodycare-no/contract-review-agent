"""LangChain ReAct 引擎（分支 feat/langchain-react-gpu-only）。

LangChain 官方 Agent（底层 LangGraph 引擎）：
- 工具调度完全交给 langchain.agents.create_agent
  （ChatOpenAI 走 vLLM OpenAI 兼容原生 tools）；
- **不存在确定性兜底/JSON降级**——LLM 不可用就是异常上抛，任务如实失败；
  GPU 使用与否从此不可被静默绕过。
- 步数预算由 recursion_limit 承担；工具业务错误原样回传给模型自纠，
  绝不静默伪造成功。
"""
from __future__ import annotations

import os
import re

from sqlalchemy.orm import Session

from app.models import ApprovalTask
from app.tools_registry import TOOLS_SCHEMA  # noqa: F401 —— schema 单一事实源（对齐测试引用）

_THINK = re.compile(r"<think>.*?</think>", re.S)

# ===== Skill 模块化：方法论以片段形式挂载，辅助而非决策 =====
SKILL_REVIEW_METHOD = (
    "【方法论 skill：合同审查推荐路径】"
    "① get_contract_approval 核对单据与附件清单 → ② download_contract_attachment 取件 → "
    "③ parse_contract_document 得到结构化字段与条款 → "
    "④ 需要时 search_contract_text 按关键词定位条款原文（上下文有限，优先检索而非通读）→ "
    "⑤ list_review_rules 浏览公司规则库作参考线索，run_contract_rules 可跑一遍初筛 → "
    "⑥ save_review_result 保存你亲笔撰写的意见 → ⑦ write_approval_comment 写回闭环。"
)

SYSTEM_PROMPT = (
    "你是企业合同审批审查Agent。\n"
    + SKILL_REVIEW_METHOD + "\n"
    "【自主决策】上述是推荐路径而非铁律——请根据单据实际情况自主决定"
    "调用哪些工具、以何顺序（例如缺附件单应尽早暴露问题，小单据可少走步骤）。"
    "是否调用、调用什么、传什么参数，由你逐轮判断。\n"
    "【规则库定位】规则是给 AI 的检索工具，不是结论：命中与建议仅作参考线索，"
    "你必须回到合同原文逐条核实，结合自身常识独立判断。\n"
    "【意见要求】最终审查意见完全由你撰写：引用条款原文佐证，指出风险并给出可执行建议。"
    "评论文本第一行必须是『总风险等级：高|中|低』，全程中文。"
    "无论前面发生什么，最后一步必须调用 write_approval_comment 完成写回——未写回即任务失败。\n"
    "【模型自觉】你是 8B 级本地模型：上下文有限，不要试图一次读完整份合同，"
    "善用检索工具定位关键条款；不确定就如实说明，禁止编造条款原文。"
)


def _chat_model():
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=os.environ.get("LLM_MODEL", "qwen3-8b"),
        api_key=os.environ.get("LLM_API_KEY", "sk-atguigu"),
        base_url=os.environ["LLM_BASE_URL"].rstrip("/"),
        temperature=0.2,
        max_retries=2,      # 同端点重试≠兜底：重试耗尽照样异常上抛
        timeout=int(os.environ.get("LLM_TIMEOUT_S", "120")),
    )


_DESC = {
    "get_contract_approval": "查看审批单详情：基本信息/表单字段/附件清单",
    "download_contract_attachment": "下载全部合同附件到本地存储（先于解析必做）",
    "parse_contract_document": "解析合同正文，产出八字段与八类条款结构",
    "run_contract_rules": "执行规则库初筛，返回命中/风险等级/关注点（仅参考线索，非结论）",
    "list_review_rules": "浏览公司规则库清单（可带关键词过滤）——规则是给AI的检索工具，"
                         "是否参考、参考哪些由你决定",
    "search_contract_text": "在已解析的合同原文中按关键词定位条款原文片段——"
                            "上下文有限时优先用它，别凭记忆复述条款",
    "save_review_result": "保存审查结果（comment_text 必须是你亲笔撰写的完整意见，"
                          "缺失会被拒绝），生成 review_id",
    "write_approval_comment": "将最终意见写回审批单评论区（闭环终点）",
}


def _tool_arg_models() -> dict:
    """从 TOOLS_SCHEMA（单一事实源）动态生成每个工具的强类型参数模型。

    此前的空属性 schema(extra=allow) 会把模型传的实参整体吞掉——
    真机联调暴露：search_contract_text 连续 VALIDATION_ERROR。
    """
    from typing import Optional

    from pydantic import BaseModel, create_model

    class _ExtraAllow(BaseModel):
        model_config = {"extra": "allow"}

    _PY = {"string": str, "integer": int, "number": float,
           "boolean": bool, "array": list, "object": dict}
    models = {}
    for t in TOOLS_SCHEMA:
        fn = t["function"]
        props = fn["parameters"].get("properties", {})
        req = set(fn["parameters"].get("required", []))
        fields = {}
        for pname, spec in props.items():
            py = _PY.get(spec.get("type"), str)
            fields[pname] = (py, ...) if pname in req else (Optional[py], None)
        models[fn["name"]] = create_model(f"{fn['name']}_Args",
                                          __base__=_ExtraAllow, **fields)
    return models


_ARG_MODELS = _tool_arg_models()


def _lc_tools(ctx):
    """把九工具执行器包装为 LangChain StructuredTool（复用统一包络）。"""
    from langchain_core.tools import StructuredTool

    def mk(name: str):
        return StructuredTool.from_function(
            coroutine=None,
            func=lambda **kwargs: _run_tool(ctx, name, kwargs),
            name=name, description=_DESC.get(name, name),
            args_schema=_ARG_MODELS.get(name))

    return [mk(n) for n in (
        "get_contract_approval", "download_contract_attachment",
        "parse_contract_document", "search_contract_text",
        "list_review_rules", "run_contract_rules",
        "save_review_result", "write_approval_comment")]


def _run_tool(ctx, name: str, args: dict) -> str:
    """统一包络执行一个工具；trace 由 execute_tool 包络层统一记账（单一事实源）。"""
    from app.tools_registry import execute_tool

    return execute_tool(ctx, name, args or {})[:2000]


def _final_text(messages: list) -> str:
    """取最后一条有内容的消息，剥掉 Qwen3 思考块。"""
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if isinstance(content, str) and content.strip():
            return _THINK.sub("", content).strip()
    return ""


def run_lc(db_session: Session, task: ApprovalTask, *, dry_run: bool = False) -> dict:
    """单任务全闭环。LLM/图执行任何失败都会异常上抛（无降级）。"""
    from langchain.agents import create_agent

    from app.core.config import get_settings
    from app.services.approval_store import get_detail

    s = get_settings()
    ctx = _ctx_new(db_session, task, dry_run)
    tools = _lc_tools(ctx)

    agent = create_agent(
        _chat_model(),
        tools,
        system_prompt=SYSTEM_PROMPT,
    )
    detail = get_detail(task.instance_id)
    brief = (f"审批单 {task.instance_id}「{task.approval_title}」"
             f"附件数={len(detail.get('attachments', []))}。开始闭环处理。"
             f"{'（dry-run：最终写入将被拦截）' if dry_run else ''}")

    raw = agent.invoke(
        {"messages": [("user", brief)]},
        config={"recursion_limit": max(16, int(s.agent_max_steps) * 2)},
    )
    messages = raw.get("messages", [])

    def _closed() -> bool:
        return bool(ctx.review_id) if dry_run else bool(ctx.written)

    # 修复轮：模型漏写回但已保存结果 → 显式纠偏一次（仍是模型决策，轨迹如实记账）
    if not _closed() and not dry_run and ctx.review_id:
        repair = agent.invoke(
            {"messages": [*messages,
             ("user", "你还没有把审查意见写回审批单评论区——没有写回即任务失败。"
                      "请立即调用 write_approval_comment 完成写回，不要再输出总结文字。")]},
            config={"recursion_limit": max(8, int(s.agent_max_steps))})
        messages = repair.get("messages", messages)

    # 闭环验证：图跑完≠闭环。未保存/未写回就返回 succeeded 是假成功——零容忍。
    if dry_run:
        closed, missing = bool(ctx.review_id), "审查结果未保存"
    else:
        closed, missing = bool(ctx.written), "审查意见未写回审批单"
    if not closed:
        tail = " → ".join(f"{i['tool']}({i['outcome']})" for i in ctx.trace[-5:]) \
               or "（一次工具都没调用）"
        raise RuntimeError(f"AI 未完成审查闭环（{missing}）——轨迹尾部: {tail}")

    return {"status": "succeeded", "steps": len(messages),
            "raw_output": _final_text(messages)[:600],
            "trace": list(ctx.trace)}


def _ctx_new(db, task, dry_run):
    from app.tools_registry import RunContext

    return RunContext(db=db, task=task, dry_run=dry_run)
