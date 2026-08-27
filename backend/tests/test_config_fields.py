"""配置字段钉死（回归锁）：llm_client 引用的设置项必须真实存在。

事故背景：2026-08 真机联调发现，llm_client 使用 settings.mock_timeout_s，
但两分支的 config.py 都没定义过它——AI 审查层每次调用都 AttributeError，
被上层静默吞成「ai_review skipped」日志。真机 LLM 从未走过该层。
"""
from __future__ import annotations


def test_llm_client_required_settings_exist():
    from app.core.config import get_settings

    s = get_settings()
    # llm_client.py 直接读取的两个超时/地址字段；缺一即上抛 AttributeError
    assert s.mock_timeout_s >= 1
    assert isinstance(s.llm_base_url, str)
