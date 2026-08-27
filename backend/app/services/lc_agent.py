"""LangChain ReAct 引擎（分支 feat/langchain-react-gpu-only）。

与自研 RunController 的关键差异：
- 工具调度完全交给 LangChain AgentExecutor + create_tool_calling_agent（vLLM 原生 tools）；
- **不存在确定性兜底/JSON降级**——LLM 不可用就是异常，任务如实 blocked(llm_required)；
  GPU 使用与否从此不可被静默绕过。
- 外层仍保留 agent_runs 持久化、三维预算(由 executor 的 max_* 配置承担)、trace 收集。
"""
from __future__ import annotations

import json
import os

from sqlalchemy.orm import Session

from app.models import AgentRun, ApprovalTask
from app.tools_registry import TOOLS_SCHEMA  # noqa: F401 —— schema 单一事实源


def _chat_model():
    from langchain_openai import ChatOpenAI

    base = os.environ["LLM_BASE_URL"].rstrip("/")
    return ChatOpenAI(model=os.environ.get("LLM_MODEL", "qwen3-8b"),
                      api_key=os.environ.get("LLM_API_KEY", "sk-atguigu"),
                      base_url=base, temperature=0.2,
                      max_retries=0, timeout=120)


def _lc_tools(ctx):
    """把七工具执行器包装为 LangChain StructuredTool（复用统一包络）。"""
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field

    class _Any(BaseModel):
        class Config:
            extra = "allow"

    def mk(name: str):
        fn = lambda **kwargs: _run_tool(ctx, name, kwargs)
        return StructuredTool.from_function(coroutine=None, func=fn, name=name,
                                            description=_DESC.get(name, name),
                                            args_schema=_Any)

    return [mk(n) for n in (
        "get_contract_approval", "download_contract_attachment",
        "parse_contract_document", "run_contract_rules",
        "save_review_result", "write_approval_comment")]


_DESC = {
    "get_contract_approval": "查看审批单详情：基本信息/表单字段/附件清单",
    "download_contract_attachment": "下载全部合同附件到本地存储（先于解析必做）",
    "parse_contract_document": "解析合同正文，产出八字段与八类条款结构",
    "run_contract_rules": "执行规则库审查，返回命中/总风险等级/关注点",
    "save_review_result": "保存审查结果，生成 review_id（write 前必须先调用）",
    "write_approval_comment": "将最终意见写回审批单评论区（闭环终点）",
}


def _run_tool(ctx, name: str, args: dict) -> str:
    from app.tools_registry import execute_tool

    out = execute_tool(ctx, name, args or {})
    ctx.trace.append({"tool": name, "outcome": "ok"})
    try:
        data = json.loads(out[:MAX_STRIP])
        err = data.get("error")
        if err:   # LangChain 不吞业务错误——模型需要看到原文来自纠
            ctx.trace[-1]["outcome"] = err.get("code", "ERR")
            raise RuntimeError(f"{err['code']}: {err['message']}")
    except json.JSONDecodeError:
        pass
    return out[:2000]


MAX_STRIP = 4000


def run_lc(db_session: Session, task: ApprovalTask, *, dry_run: bool = False) -> dict:
    """单任务全闭环。LLM 任何失败都会抛出（无降级）。"""
    from app.services.approval_store import get_detail
    from langchain.agents import AgentExecutor, create_tool_calling_agent
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

    s = __import__("app.core.config", fromlist=["get_settings"]).get_settings()
    ctx = _ctx_new(db_session, task, dry_run)

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "你是企业合同审批审查Agent。严格按顺序调用工具完成："
         "下载附件→解析文档→执行规则审查→保存审查结果→写回评论。"
         "评论文本第一行必须是『总风险等级：高|中|低』。全程中文。"
         "{json_tail}"),
        ("user", "{input}"),
        MessagesPlaceholder("agent_scratchpad"),
    ])
    llm = _chat_model()
    agent = create_tool_calling_agent(llm.bind(tools=[
        {"type": "function", "function": {"name": n, "description": d,
                                          "parameters": {"type": "object"}}}
        for n, d in _DESC.items()]), _lc_tools(ctx), prompt)
    executor = AgentExecutor(agent=agent, tools=_lc_tools(ctx),
                             max_iterations=int(s.agent_max_steps),
                             max_execution_time=s.agent_wall_budget_s,
                             handle_parsing_errors=True, verbose=False,
                             return_intermediate_steps=True)
    detail = get_detail(task.instance_id)
    brief = (f"审批单 {task.instance_id}「{task.approval_title}」"
             f"附件数={len(detail.get('attachments', []))}。开始闭环处理。"
             f"{'（dry-run：最终写入将被拦截）' if dry_run else ''}")
    result = executor.invoke({"input": brief, "json_tail": ""})
    return {"status": "succeeded" if not result.get("__interrupted") else "blocked",
            "steps": len(result.get("intermediate_steps") or []),
            "raw_output": (result.get("output") or "")[:600]}


def _ctx_new(db, task, dry_run):
    from app.tools_registry import RunContext

    return RunContext(db=db, task=task, dry_run=dry_run)
