"""mock 审批系统 HTTP 客户端：GET 幂等自动重试，POST 不重试（SDD §7.5）。"""
from __future__ import annotations

import httpx

from app.core.config import get_settings
from app.services.tool_errors import ToolError


def _client() -> httpx.Client:
    settings = get_settings()
    transport = httpx.HTTPTransport(retries=2)  # 仅 GET 类幂等请求使用
    return httpx.Client(base_url=settings.mock_base_url,
                        timeout=settings.mock_timeout_s,
                        transport=transport)


def _unwrap(body):
    """兼容 mock 的 {code,data} 信封与裸数组/裸对象三种形态。"""
    if isinstance(body, dict) and "data" in body:
        body = body["data"]
    return body


def list_pending(limit: int = 20) -> list[dict]:
    try:
        with _client() as c:
            r = c.get("/mock/approvals", params={"limit": limit})
            r.raise_for_status()
            body = _unwrap(r.json())
            if isinstance(body, dict):
                body = body.get("items", [])
            return body
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ToolError("MOCK_UNREACHABLE", f"拉取待办失败: {exc}", retriable=True) from exc


def get_detail(instance_id: str) -> dict:
    try:
        with _client() as c:
            r = c.get(f"/mock/approvals/{instance_id}")
            if r.status_code == 404:
                raise ToolError("APPROVAL_NOT_FOUND", f"审批单不存在: {instance_id}")
            r.raise_for_status()
            return _unwrap(r.json())
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ToolError("MOCK_UNREACHABLE", f"详情获取失败: {exc}", retriable=True) from exc


def download_attachment(instance_id: str, attachment_id: str) -> tuple[bytes, str]:
    """返回 (bytes, file_name)；文件名取 Content-Disposition（RFC5987 兼容由 mock 保证）。"""
    try:
        with _client() as c:
            r = c.get(f"/mock/approvals/{instance_id}/attachments/{attachment_id}")
            if r.status_code == 404:
                raise ToolError("ATTACHMENT_MISSING",
                                f"附件不存在: {instance_id}/{attachment_id}",
                                block_stage="parsing")
            r.raise_for_status()
            name = attachment_id + ".bin"
            disp = r.headers.get("content-disposition", "")
            for part in disp.split(";"):
                part = part.strip()
                if part.startswith("filename*="):
                    from urllib.parse import unquote

                    _, _, value = part.partition("=")
                    encoding, _, quoted = value.partition("''")
                    name = unquote(quoted, encoding or "utf-8")
                    break
                if part.startswith("filename="):
                    name = part.partition("=")[2].strip('"')
            return r.content, name
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ToolError("MOCK_UNREACHABLE", f"附件下载失败: {exc}", retriable=True) from exc


def post_comment(instance_id: str, comment_text: str) -> dict:
    """非幂等：不自动重试。"""
    try:
        with _client() as c:
            r = c.post(f"/mock/approvals/{instance_id}/comments",
                       json={"comment_text": comment_text})
            if r.status_code >= 500:
                raise ToolError("WRITE_FAILED", f"评论接口 5xx: {r.status_code}",
                                block_stage="reviewing")
            r.raise_for_status()
            return r.json()
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ToolError("WRITE_FAILED", f"评论回写失败: {exc}",
                        block_stage="reviewing") from exc


def reset_mock() -> None:
    try:
        with _client() as c:
            c.post("/mock/reset")
    except Exception as exc:  # noqa: BLE001
        raise ToolError("MOCK_UNREACHABLE", f"reset 失败: {exc}", retriable=True) from exc
