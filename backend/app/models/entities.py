"""八张业务表实体定义（规范 §2.4.9）。"""
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, Integer, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ApprovalTask(TimestampMixin, Base):
    __tablename__ = "approval_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    approval_code: Mapped[str] = mapped_column(String(64), unique=True)
    approval_title: Mapped[str] = mapped_column(String(255))
    applicant_name: Mapped[str] = mapped_column(String(64), default="")
    instance_id: Mapped[str] = mapped_column(String(64))
    task_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    write_status: Mapped[str] = mapped_column(String(16), default="not_written")
    block_reason: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)


class ApprovalAttachment(Base):
    __tablename__ = "approval_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    attachment_id: Mapped[str] = mapped_column(String(64))
    file_name: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(16), default="")
    file_path: Mapped[str] = mapped_column(String(512))
    download_status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ContractParse(Base):
    __tablename__ = "contract_parses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    basic_info_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    clause_info_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    parse_status: Mapped[str] = mapped_column(String(16), default="pending")
    parse_error: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ReviewRule(TimestampMixin, Base):
    __tablename__ = "review_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_code: Mapped[str] = mapped_column(String(64), unique=True)
    rule_name: Mapped[str] = mapped_column(String(128))
    risk_level: Mapped[str] = mapped_column(String(8))          # high|medium|low
    rule_status: Mapped[int] = mapped_column(SmallInteger, default=1)
    match_mode: Mapped[str] = mapped_column(String(16))         # keyword|regex|absence
    match_text: Mapped[str] = mapped_column(Text)
    suggestion_text: Mapped[str] = mapped_column(String(512))


class RuleHit(Base):
    __tablename__ = "rule_hits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    rule_id: Mapped[int] = mapped_column(Integer, index=True)
    evidence_text: Mapped[str] = mapped_column(String(1024))
    evidence_position: Mapped[str] = mapped_column(String(64), default="")
    hit_status: Mapped[str] = mapped_column(String(16), default="hit")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ReviewResult(Base):
    __tablename__ = "review_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    overall_risk_level: Mapped[str] = mapped_column(String(8))
    summary_text: Mapped[str] = mapped_column(Text)
    focus_points_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    comment_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CommentLog(Base):
    __tablename__ = "comment_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    write_status: Mapped[str] = mapped_column(String(16))
    write_response_text: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TaskLog(Base):
    __tablename__ = "task_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    log_level: Mapped[str] = mapped_column(String(8), default="info")
    log_type: Mapped[str] = mapped_column(String(32), default="")
    log_content: Mapped[str] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AgentRun(TimestampMixin, Base):
    """Agent 运行记录（规范八表之外的工程超集，偏差已登记）。

    支撑断点恢复(messages_json 快照)、预算审计(steps/tokens/wall)、降级溯源(fallback_kind)。
    """

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    channel: Mapped[str] = mapped_column(String(16), default="pending")   # native|json|deterministic|pending
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)  # running|succeeded|blocked|failed
    dry_run: Mapped[int] = mapped_column(SmallInteger, default=0)
    steps_used: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    wall_ms: Mapped[int] = mapped_column(Integer, default=0)
    fallback_kind: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    model_name: Mapped[str] = mapped_column(String(64), default="")
    messages_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    error_digest: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
