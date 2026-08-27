"""RunController：生产级 Agent 运行时（ADR-B7/B8）。

三维预算(步数/token/墙钟)优雅终结 · 熔断器 · 通道降级阶梯 native→json→deterministic ·
每步消息快照持久化(断点恢复) · dry-run · 幂等回写由工具层保证。
"""
from __future__ import annotations

import re
import time

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.obs import (FALLBACKS, RUN_LATENCY, RUNS_TOTAL, CircuitBreaker,
                          get_circuit_breaker, get_logger, log_event)
from app.core.prompts import active_version
from app.models import AgentRun, ApprovalTask
from app.services import llm_client, reviewer
from app.services.llm_client import LLMUnavailable, RealHTTPTransport
from app.services.tool_errors import ToolError, to_blocked_stage
from app.tools_registry import (RunContext, build_system_prompt,
                                execute_tool, parse_protocol_line,
                                run_deterministic, tools_for_channel,
                                user_briefing)

logger = get_logger("runcontroller")

_CHANNEL_FALLBACK = {"native": "json", "json": "deterministic"}

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def _new_transport():
    settings = get_settings()
    if not settings.llm_base_url:
        return None
    transport = RealHTTPTransport()
    if settings.record_trajectory:
        from app.services.llm_client import RecordingTransport

        return RecordingTransport(transport, settings.record_trajectory)
    return transport


class RunController:
    def __init__(self, db: Session, task: ApprovalTask, *,
                 dry_run: bool = False, transport=None,
                 breaker: CircuitBreaker | None = None) -> None:
        self.db = db
        self.task = task
        self.dry_run = dry_run
        self.settings = get_settings()
        self.breaker = breaker or get_circuit_breaker()
        self.transport = transport if transport is not None else _new_transport()
        self.ctx = RunContext(db=db, task=task, dry_run=dry_run)
        self.steps: list[dict] = []

    # ---------- 入口 ----------

    def start(self) -> AgentRun:
        conflict = self.db.query(AgentRun).filter_by(
            task_id=self.task.id, status="running").one_or_none()
        if conflict is not None:
            raise ToolError("RUN_CONFLICT",
                            f"任务已有运行中的 run #{conflict.id}；可调用 resume")
        run = AgentRun(task_id=self.task.id, channel="pending", dry_run=int(self.dry_run),
                       prompt_version=active_version("agent_system"),
                       model_name=self.settings.llm_model)
        self.db.add(run)
        self.db.commit()
        return self._execute(run)

    def resume(self, run_id: int) -> AgentRun:
        run = self.db.query(AgentRun).filter_by(id=run_id).one_or_none()
        if run is None:
            raise ToolError("RUN_NOT_FOUND", f"run 不存在: {run_id}")
        if run.status != "running":
            raise ToolError("INVALID_STATE", f"run 状态 {run.status} 不可恢复")
        self._restore_ctx_from(run)
        log_event(logger, 20, "run resumed", run_id=run.id, task_id=self.task.id)
        return self._execute(run)

    def _restore_ctx_from(self, run: AgentRun) -> None:
        self.run = run
        self.messages = list(run.messages_json or [])
        self.channel = run.channel if run.channel in ("native", "json") else "json"
        self.fallback_kind = run.fallback_kind
        # 已完成标记依据快照重放：直接重建 written/review 判定于循环内自然恢复

    # ---------- 主循环 ----------

    def _execute(self, run: AgentRun) -> AgentRun:
        started = time.monotonic()
        self.run = run
        s = self.settings
        wall_deadline = started + s.agent_wall_budget_s
        prompt_tokens = completion_tokens = llm_calls = steps_used = 0
        self.messages = [{"role": "system", "content": build_system_prompt("native")},
                         {"role": "user", "content": user_briefing(self.task)}]
        self.fallback_kind = getattr(self, "fallback_kind", None)
        channel = getattr(self, "channel", None) or self._pick_channel()
        run.channel = channel
        self.db.commit()

        status = "failed"
        error_digest: str | None = None
        try:
            if channel == "deterministic":
                # 确定性通道：整个计划仅执行一次，不进入模型循环（ADR-B5）
                run_deterministic(self.ctx)
                steps_used += 1
                self.messages.append({"role": "assistant",
                                      "content": "[确定性通道] 六步计划执行完毕"})
            while True:
                if channel == "deterministic":
                    break
                if steps_used >= s.agent_max_steps:
                    self.fallback_kind = self.fallback_kind or "budget_steps"
                    break
                # token 预算只计"生成侧"消耗——prompt 累积是对话循环的固有成本，
                # 不属于模型挥霍；完成侧上限才是可控变量（SDD §7.4）
                if completion_tokens >= s.agent_token_budget:
                    self.fallback_kind = self.fallback_kind or "budget_tokens"
                    break
                if time.monotonic() >= wall_deadline:
                    self.fallback_kind = self.fallback_kind or "budget_wall"
                    break

                message = self._chat(channel)
                llm_calls += 1
                usage = getattr(self, "_last_usage", {}) or {}
                prompt_tokens += int(usage.get("prompt_tokens") or 0)
                completion_tokens += int(usage.get("completion_tokens") or 0)
                steps_used += 1

                # 防御性剥离残留思考块（服务端未关 thinking 时保证 JSON 协议与快照干净）
                raw_content = message.get("content")
                if raw_content:
                    message["content"] = _THINK_RE.sub("", raw_content).strip()
                tool_calls = message.get("tool_calls") or []
                content = message.get("content") or ""
                executed_write = False
                if tool_calls:
                    for call in tool_calls:
                        fn = call.get("function", {})
                        args = _safe_args(fn.get("arguments"))
                        result = execute_tool(self.ctx, fn.get("name", ""), args)
                        executed_write |= fn.get("name") == "write_approval_comment" and \
                            '"write_status": "success"' in result
                        self.messages.append({"role": "tool", "name": fn.get("name"),
                                              "content": result})
                elif content:
                    tool, args = parse_protocol_line(content)
                    if tool is None:
                        executed_write |= False
                        if channel == "json" and content.strip():
                            break  # 模型给出 final → 进入 finalize
                    else:
                        result = execute_tool(self.ctx, tool, args)
                        executed_write |= tool == "write_approval_comment" and \
                            '"write_status": "success"' in result
                        self.messages.append({"role": "user", "content": f"[工具结果] {result}"})
                else:
                    break

                self._snapshot(run, steps_used, prompt_tokens + completion_tokens,
                               llm_calls, started, channel)
                if executed_write and self.ctx.written:
                    status = "succeeded"
                    break
                if channel == "native" and not tool_calls and "final" not in content:
                    continue  # 纯文本闲聊，继续下一轮
                if self.ctx.written:
                    status = "succeeded"
                    break
            else:
                status = "failed"
        except LLMUnavailable as exc:
            self.breaker.record_failure()
            downgraded = _CHANNEL_FALLBACK.get(channel)
            if downgraded == "deterministic":
                log_event(logger, 30, "channel downgrade", run_id=run.id,
                          kind=f"{channel}->deterministic", err=str(exc)[:160])
                run.channel = self.channel = "deterministic"
                self.fallback_kind = self.fallback_kind or "llm_down"
                FALLBACKS.labels(kind="llm_down").inc()
                self.db.commit()
                run_deterministic(self.ctx)   # 就地执行确定性计划（ADR-B5）
                steps_used += 1
                channel = "deterministic"
                status = "succeeded" if self.ctx.written else "failed"
            elif downgraded:
                log_event(logger, 30, "channel downgrade", run_id=run.id,
                          kind=f"{channel}->{downgraded}", err=str(exc)[:160])
                run.channel = self.channel = downgraded
                self.fallback_kind = self.fallback_kind or "llm_down"
                FALLBACKS.labels(kind="llm_down").inc()
                self.db.commit()
                return self._execute(run)   # 以降级通道重启同一 run
            else:
                self.fallback_kind = "llm_down"
                status, error_digest = "blocked", f"LLM_UNAVAILABLE: {exc}"[:500]
        except Exception as exc:  # noqa: BLE001
            status, error_digest = "failed", f"{type(exc).__name__}: {exc}"[:500]

        if status == "failed" and self.ctx.written:
            status = "succeeded"
        if status not in ("succeeded", "blocked"):
            status = self._finalize(error_digest)

        self._snapshot(run, steps_used, prompt_tokens + completion_tokens,
                       llm_calls, started, channel, final=True,
                       status=status, error_digest=error_digest)
        RUNS_TOTAL.labels(channel=channel, status=status).inc()
        RUN_LATENCY.observe(time.monotonic() - started)
        log_event(logger, 20, "run finished", run_id=run.id, task_id=self.task.id,
                  kind=f"{status}/{channel}/fallback={self.fallback_kind}")
        return run

    # ---------- 辅助 ----------

    def _pick_channel(self) -> str:
        allowed, state = self.breaker.allow()
        if not allowed:
            FALLBACKS.labels(kind="circuit_open").inc()
            self.fallback_kind = self.fallback_kind or "circuit_open"
            return "deterministic"
        if self.transport is None:
            FALLBACKS.labels(kind="llm_down").inc()
            self.fallback_kind = self.fallback_kind or "llm_down"
            return "deterministic"
        try:
            # 探测走底层裸传输（录制态下旁路 RecordingTransport，避免污染轨迹）
            probe_target = getattr(self.transport, "inner", self.transport)
            return "native" if llm_client.probe_native_tools(probe_target) else "json"
        except Exception:  # noqa: BLE001
            return "json"

    def _chat(self, channel: str) -> dict:
        if channel == "deterministic":
            outputs = run_deterministic(self.ctx)
            self.messages.append({"role": "assistant",
                                  "content": "[确定性通道] " + " | ".join(outputs[-2:])})
            return {"content": '{"final":"deterministic"}', "tool_calls": None}
        allowed, _ = self.breaker.allow()
        if not allowed:
            self.channel = channel = "deterministic"
            return self._chat(channel)
        try:
            message = llm_client.counted_chat(self.transport, self.messages,
                                              tools_for_channel(channel), channel)
            self.breaker.record_success()
            self._last_usage = message.pop("_usage", None) or {}
            return message
        except LLMUnavailable as exc:
            self.breaker.record_failure()
            raise

    def _finalize(self, error_digest: str | None) -> str:
        """优雅终结：以已采集数据强制 save+write（dry_run 跳过外呼）。"""
        if self.ctx.written:
            return "succeeded"
        summary = self.ctx.rules_summary or {"overall_risk_level": "low",
                                             "overall_risk_label": "低",
                                             "hits": [], "focus_points": []}
        comment = reviewer.build_comment_text(summary, None)
        try:
            review = None
            if self.ctx.review_id:
                from app.models import ReviewResult

                review = self.db.query(ReviewResult).filter_by(
                    id=self.ctx.review_id).one_or_none()
            if review is None:
                review = reviewer.save_result(
                    self.db, self.task,
                    overall_risk_level=summary["overall_risk_level"],
                    summary_text=f"兜底生成：命中 {len(summary['hits'])} 条规则。",
                    focus_points_json=summary["focus_points"], comment_text=comment)
                self.ctx.review_id = review.id
            if self.dry_run:
                log_event(logger, 20, "dry_run skip write", run_id=self.run.id)
                return "succeeded"
            outcome = reviewer.write_comment(self.db, self.task, review)
            self.ctx.written = outcome.get("write_status") == "success"
            return "succeeded" if self.ctx.written else "blocked"
        except ToolError as exc:
            code = exc.code if exc.code in ("ATTACHMENT_MISSING", "PARSE_EMPTY",
                                            "OCR_FAILED", "WRITE_FAILED") else "WRITE_FAILED"
            stage = to_blocked_stage(code)
            from app.services.state_machine import block_task

            block_task(self.db, self.task, code, str(exc))
            return "blocked"

    def _snapshot(self, run: AgentRun, steps_used: int, tokens: int, llm_calls: int,
                  started: float, channel: str, *, final: bool = False,
                  status: str | None = None, error_digest: str | None = None) -> None:
        run.steps_used = steps_used
        run.prompt_tokens = tokens // 2
        run.completion_tokens = tokens - tokens // 2
        run.llm_calls = llm_calls
        run.wall_ms = int((time.monotonic() - started) * 1000)
        run.channel = channel
        run.messages_json = self.messages[-14:]
        run.fallback_kind = self.fallback_kind
        if final:
            run.status = status or "failed"
            run.error_digest = error_digest
            from datetime import datetime

            run.finished_at = datetime.now()
            if status == "succeeded" and not self.dry_run:
                self.task.write_status = self.task.write_status
        self.db.commit()


def _safe_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        import json

        return json.loads(raw or "{}")
    except Exception:  # noqa: BLE001
        return {}
