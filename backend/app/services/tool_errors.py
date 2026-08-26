"""工具层错误分类学（SDD §7.5）：code → retriable → blocked 映射。"""


class ToolError(Exception):
    def __init__(self, code: str, message: str, *, retriable: bool = False,
                 block_stage: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retriable = retriable
        self.block_stage = block_stage  # 进入 blocked 后 retry 应回溯的阶段


BLOCKED_CODES = {
    "ATTACHMENT_MISSING": "parsing",
    "PARSE_EMPTY": "parsing",
    "OCR_FAILED": "parsing",
    "WRITE_FAILED": "reviewing",
}


def to_blocked_stage(code: str, default: str = "parsing") -> str:
    return BLOCKED_CODES.get(code, default)
