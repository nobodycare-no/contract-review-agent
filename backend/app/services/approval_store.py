"""统一系统本地审批域网关（V1-S2 架构合并后唯一实现）。

与原外部模拟客户端保持同形签名，调用方零语义漂移；底层改为本系统自有
业务表(approval_tasks/approval_attachments)+附件磁盘目录。

约定：
- instance_id 即审批单的业务实例标识（演示种子为 LOCAL-AP-2026-00x，手工上传自动生成）；
- 附件上传即落盘至 UPLOAD_DIR/source/ 并登记行（download_status 直接 done）；
- post_comment 仅作回写确认——意见正文的权威副本由调用方写入
  review_results/comment_logs（八表契约不变）。
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from app.core.config import get_settings
from app.models import ApprovalAttachment, ApprovalTask
from app.services.tool_errors import ToolError


def _db():
    from app.db import SessionLocal

    return SessionLocal()


# ---------- 查询 ----------

def list_pending(limit: int = 20) -> list[dict]:
    with _db() as db:
        rows = (db.query(ApprovalTask).filter_by(task_status="pending")
                .order_by(ApprovalTask.id).limit(limit).all())
        return [{
            "instance_id": r.instance_id,
            "approval_code": r.approval_code,
            "approval_title": r.approval_title,
            "applicant_name": r.applicant_name,
            "apply_time": str(r.created_at)[:19],
            "attachment_count": db.query(ApprovalAttachment).filter_by(task_id=r.id).count(),
        } for r in rows]


def get_detail(instance_id: str) -> dict:
    with _db() as db:
        t = db.query(ApprovalTask).filter_by(instance_id=instance_id).one_or_none()
        if t is None:
            raise ToolError("APPROVAL_NOT_FOUND", f"审批单不存在: {instance_id}")
        atts = db.query(ApprovalAttachment).filter_by(task_id=t.id).all()
        return {
            "instance_id": t.instance_id,
            "approval_code": t.approval_code,
            "title": t.approval_title,
            "applicant": t.applicant_name,
            "apply_time": str(t.created_at)[:19],
            "form_data": {"来源": "本系统录入"},
            "status": t.task_status,
            "attachments": [{"attachment_id": a.attachment_id,
                             "file_name": a.file_name,
                             "file_type": a.file_type,
                             "file_path": a.file_path,
                             "download_status": a.download_status} for a in atts],
        }


def resolve_local_path(row: ApprovalAttachment) -> Path | None:
    """已有物理文件时返回绝对路径（本地上传模式下≈总是有）。"""
    p = Path(row.file_path or "")
    return p if p.is_file() else None


def download_attachment(instance_id: str, attachment_id: str) -> tuple[bytes, str]:
    """读取已在本系统盘上的附件字节流。缺失=ATTACHMENT_MISSING(blocked 语义)。"""
    with _db() as db:
        row = db.query(ApprovalAttachment).filter_by(attachment_id=attachment_id).one_or_none()
        if row is None:
            raise ToolError("ATTACHMENT_MISSING", f"未知附件: {attachment_id}",
                            block_stage="parsing")
        local = resolve_local_path(row)
    if local is None:
        raise ToolError("ATTACHMENT_MISSING", f"附件文件不在盘上: {row.file_name}",
                        block_stage="parsing")
    content = local.read_bytes()
    digest = hashlib.sha256(content).hexdigest()[:12]
    from app.core.obs import log_event, get_logger

    log_event(get_logger("approval_store"), 20, "attachment read",
              kind=f"{row.file_name}:{digest}:{len(content)}B")
    return content, row.file_name


def materialize_into_task_dir(task_id: int) -> int:
    """把源文件规范化复制到该任务目录并置 done（兼容旧下载语义，SHA 记日志）。"""
    s = get_settings()
    with _db() as db:
        task = db.query(ApprovalTask).filter_by(id=task_id).one()
        rows = db.query(ApprovalAttachment).filter_by(task_id=task_id).all()
        target_dir = Path(s.upload_dir) / str(task_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        for row in rows:
            src = resolve_local_path(row)
            if src is None:
                raise ToolError("ATTACHMENT_MISSING", f"源文件丢失: {row.file_name}",
                                block_stage="parsing")
            dst = target_dir / Path(src).name
            if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                shutil.copyfile(src, dst)
            row.file_path = str(dst)
            row.download_status = "done"
            content = dst.read_bytes()
            from app.core.obs import get_logger, log_event

            log_event(get_logger("approval_store"), 20, "attachment staged",
                      task_id=task_id,
                      kind=f"{dst.name}:{hashlib.sha256(content).hexdigest()[:12]}:{len(content)}B")
            n += 1
        db.commit()
        return n


def post_comment(instance_id: str, comment_text: str) -> dict:
    """回写确认：统一系统中意见权威副本在 review_results/comment_logs，
    此处仅确认通道成功（真实外部对接时为 HTTP POST 的对称位）。"""
    with _db() as db:
        t = db.query(ApprovalTask).filter_by(instance_id=instance_id).one_or_none()
        if t is None:
            raise ToolError("WRITE_FAILED", f"审批单不存在: {instance_id}",
                            block_stage="reviewing")
        prior = (db.query(ApprovalAttachment).filter_by(task_id=t.id).count(), )
    return {"write_status": "success", "comment_id": 0, "prior_attachments": prior[0]}


def create_form(*, title: str, applicant: str, sources: list[tuple[str, bytes]],
                apply_time: str | None = None,
                approval_code: str | None = None,
                instance_id: str | None = None) -> ApprovalTask:
    """前台/批量入口：创建审批单并落附件文件（status=pending）。"""
    import datetime as _dt

    s = get_settings()
    stamp = f"{_dt.datetime.now():%Y%m%d}"
    size_tag = sum(len(b) for b in sources) % 9973
    with _db() as db:
        if approval_code:
            dup = db.query(ApprovalTask).filter_by(approval_code=approval_code) \
                .one_or_none()
            if dup is not None:
                raise ToolError("VALIDATION_ERROR", f"审批编号重复: {approval_code}")
            code = approval_code
        else:
            stem = abs(hash(f"{title}")) % 9000
            base = f"AP-LOCAL-{stamp}-{stem:04d}-{size_tag:04d}"
            code, attempt = base, 1
            while db.query(ApprovalTask).filter_by(approval_code=code) \
                    .one_or_none() is not None:
                attempt += 1
                if attempt > 8:
                    code = f"{base}-{_dt.datetime.now().strftime('%H%M%S%f')[-6:]}"
                    break
                code = f"{base}-{attempt:02d}"
        inst = instance_id or f"LOCAL-{code}"
        task = ApprovalTask(approval_code=code, approval_title=title,
                            applicant_name=applicant, instance_id=inst)
        db.add(task)
        db.flush()

        base = Path(s.upload_dir) / "sources" / str(task.id)
        base.mkdir(parents=True, exist_ok=True)
        for idx, (name, blob) in enumerate(sources, 1):
            safe = Path(name).name
            ext = Path(safe).suffix.lower().lstrip(".")
            dest = base / safe
            dest.write_bytes(blob)
            att_id = f"att-{task.id}-{idx:02d}"
            db.add(ApprovalAttachment(
                task_id=task.id, attachment_id=att_id, file_name=safe,
                file_type=ext[:15], file_path=str(dest),
                download_status="done"))
        db.commit()
        db.refresh(task)
        return task


def reset_demo() -> dict:
    """清空业务侧全部数据并重种六张演示审批单（读 deploy/demo_contracts 静态资产）。"""
    from app.models import AgentRun, CommentLog, ContractParse, ReviewResult, \
        RuleHit, TaskLog

    repo_root = Path(__file__).resolve().parents[3]
    asset_dir = repo_root / "deploy" / "demo_contracts"

    with _db() as db:
        for model in (AgentRun, CommentLog, ReviewResult, RuleHit, ContractParse,
                      ApprovalAttachment, TaskLog, ApprovalTask):
            db.query(model).delete()
        db.commit()

        seeded = []
        profiles = [
            ("LOCAL-AP-2026-001", "GPU 服务器集群采购合同审批", "王铁柱",
             [("技术采购合同.docx", asset_dir / "技术采购合同.docx")]),
            ("LOCAL-AP-2026-002", "运维服务外包协议审批", "李梅",
             [("服务外包协议.docx", asset_dir / "服务外包协议.docx")]),
            ("LOCAL-AP-2026-003", "办公设备租赁合同审批", "张伟",
             [("设备租赁合同.docx", asset_dir / "设备租赁合同.docx")]),
            ("LOCAL-AP-2026-004", "客户数据处理服务协议审批", "赵敏",
             [("数据处理协议.md", asset_dir / "数据处理协议.md")]),
            ("LOCAL-AP-2026-005", "供应商盖章页扫描件补录", "王铁柱",
             [("盖章页扫描件.png", asset_dir / "盖章页扫描件.png")]),
            ("LOCAL-AP-2026-006", "市场推广物料印刷合同（附件遗失）", "李梅", []),
        ]
        for code, title, applicant, files in profiles:
            sources = [(f.name, f.read_bytes()) for _, f in files]
            t = create_form(title=title, applicant=applicant,
                            sources=sources, approval_code=code)
            seeded.append((t.approval_code, len(files)))
    return {"reset": True, "seeded": seeded}
