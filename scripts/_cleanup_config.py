# 一次性清理：config 移除 mock 字段（执行后删除本文件）
from pathlib import Path

f = Path("backend/app/core/config.py")
t = f.read_text(encoding="utf-8")
t = t.replace("    # mock 审批系统（服务间）\n", "")
t = t.replace('    mock_base_url: str = "http://mock-approval:8100"\n', "")
t = t.replace("    mock_timeout_s: int = 15\n", "    tool_timeout_s: int = 15\n")
f.write_text(t, encoding="utf-8")
print("config cleaned:", "mock" not in t.lower())
