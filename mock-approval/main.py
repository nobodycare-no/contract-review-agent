"""mock 审批系统入口——仿真外部审批系统的四个服务入口。"""
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from store import store

app = FastAPI(title="Mock Approval System", version="0.1.0",
              description="模拟外部审批系统：待办/详情/附件/评论")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/mock/approvals")
def list_pending(limit: int = 20):
    return {"code": 0, "data": store.list_pending(limit)}


@app.get("/mock/approvals/{instance_id}")
def get_detail(instance_id: str):
    detail = store.get_detail(instance_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="审批单不存在")
    return {"code": 0, "data": detail}


@app.get("/mock/approvals/{instance_id}/attachments/{attachment_id}")
def download_attachment(instance_id: str, attachment_id: str):
    detail = store.get_detail(instance_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="实例不存在")
    att = next((a for a in detail["attachments"]
                if a["attachment_id"] == attachment_id), None)
    if att is None:
        raise HTTPException(status_code=404, detail="附件不存在")
    try:
        content, media_type = store.render_attachment(
            att["template"], att["file_name"], instance_id)
    except KeyError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    from urllib.parse import quote
    quoted = quote(att["file_name"])
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
        "X-File-Name": quoted,
    }
    return Response(content=content, media_type=media_type, headers=headers)


@app.post("/mock/approvals/{instance_id}/comments")
def write_comment(instance_id: str, payload: dict):
    comment_text = (payload or {}).get("comment_text", "")
    result = store.add_comment(instance_id, comment_text)
    if result["write_status"] != "success":
        raise HTTPException(status_code=404, detail=result.get("error"))
    return {"code": 0, "data": result}


@app.post("/mock/reset")
def reset():
    store.reset()
    return {"code": 0, "message": "mock 数据已复位"}
