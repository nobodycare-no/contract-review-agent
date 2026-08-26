"""提示词注册表加载器（G5）：version 化、模板渲染、进程级缓存。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml


@lru_cache
def registry() -> dict[str, dict]:
    path = Path(__file__).resolve().parents[1] / "prompts" / "prompts.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data, "prompts.yaml 不能为空"
    for pid, item in data.items():
        assert "version" in item and "template" in item, f"提示词 {pid} 缺 version/template"
    return data


def get(prompt_id: str) -> dict:
    return registry()[prompt_id]


def render(prompt_id: str, **kwargs: str) -> str:
    """以 {key} 占位符渲染（str.format 的安全子集：缺失占位符保留原样便于排查）。"""

    class _SafeDict(dict):
        def __missing__(self, key: str) -> str:  # noqa: D102
            return "{" + key + "}"

    template = get(prompt_id)["template"]
    return template.format_map(_SafeDict(**kwargs))


def active_version(prompt_id: str) -> str:
    return str(get(prompt_id)["version"])
