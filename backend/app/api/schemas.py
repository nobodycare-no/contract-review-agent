"""工具面 Pydantic 契约（规范 §2.4.10 签名逐一对应）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ListPendingReq(BaseModel):
    limit: int = Field(20, ge=1, le=100)


class GetApprovalReq(BaseModel):
    instance_id: str


class DownloadAttachmentReq(BaseModel):
    instance_id: str
    attachment_id: str | None = None   # None = 全量下载


class ParseDocumentReq(BaseModel):
    document_id: int                   # = task_id


class RunRulesReq(BaseModel):
    case_id: int                       # = task_id


class SaveResultReq(BaseModel):
    case_id: int
    overall_risk_level: str = Field(pattern="^(high|medium|low)$")
    summary_text: str
    focus_points_json: list[str] = []
    comment_text: str


class WriteCommentReq(BaseModel):
    instance_id: str
    review_id: int


class RetryReq(BaseModel):
    force: bool = False
