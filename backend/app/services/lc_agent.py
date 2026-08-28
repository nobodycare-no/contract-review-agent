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

SYSTEM_PROMPT = (
    "你是企业合同审批审查Agent。严格按顺序调用工具完成闭环："
    "下载附件→解析文档→执行规则审查→保存审查结果→写回评论。"
    "评论文本第一行必须是『总风险等级：高|中|低』。全程中文。"
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
    "run_contract_rules": "执行规则库审查，返回命中/总风险等级/关注点",
    "save_review_result": "保存审查结果，生成 review_id（write 前必须先调用）",
    "write_approval_comment": "将最终意见写回审批单评论区（闭环终点）",
}


def _lc_tools(ctx):
    """把七工具执行器包装为 LangChain StructuredTool（复用统一包络）。"""
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field  # noqa: F401

    class _Any(BaseModel):
        model_config = {"extra": "allow"}

    def mk(name: str):
        return StructuredTool.from_function(
            coroutine=None,
            func=lambda **kwargs: _run_tool(ctx, name, kwargs),
            name=name, description=_DESC.get(name, name), args_schema=_Any)

    return [mk(n) for n in (
        "get_contract_approval", "download_contract_attachment",
        "parse_contract_document", "run_contract_rules",
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
    return {"status": "succeeded", "steps": len(messages),
            "raw_output": _final_text(messages)[:600],
            "trace": list(ctx.trace)}


def _ctx_new(db, task, dry_run):
    from app.tools_registry import RunContext

    return RunContext(db=db, task=task, dry_run=dry_run)
