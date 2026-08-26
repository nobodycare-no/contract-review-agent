"""合同文档解析器：docx/md/txt → 全文；pdf → pypdf；png/jpg → tesseract OCR。

输出结构化字段（LLM 提取 + 正则兜底）与八类条款定位。
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

from app.core.config import get_settings


class ParseError(Exception):
    pass


def extract_raw_text(file_path: str) -> tuple[str, str]:
    """按扩展名提取全文，返回 (text, file_type)。"""
    path = Path(file_path)
    ext = path.suffix.lower()
    data = path.read_bytes()

    if ext in (".md", ".txt"):
        for enc in ("utf-8", "gb18030"):
            try:
                return data.decode(enc), ext[1:]
            except UnicodeDecodeError:
                continue
        raise ParseError("文本编码无法识别")

    if ext == ".docx":
        return _extract_docx(data), "docx"

    if ext == ".pdf":
        return _extract_pdf(data), "pdf"

    if ext in (".png", ".jpg", ".jpeg"):
        return _extract_ocr(data), ext[1:]

    raise ParseError(f"不支持的附件类型: {ext}")


def _extract_docx(data: bytes) -> str:
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as e:
        raise ParseError(f"DOCX 解析失败: {e}") from e
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    text = "\n".join(parts).strip()
    if not text:
        raise ParseError("DOCX 中未提取到任何文本")
    return text


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as e:
        raise ParseError(f"PDF 解析失败: {e}") from e
    text = "\n".join(p for p in pages if p.strip())
    if not text:
        raise ParseError("PDF 无文字层——疑似扫描件，OCR 流程请走图片路径")
    return text


def _extract_ocr(data: bytes) -> str:
    import pytesseract
    from PIL import Image

    settings = get_settings()
    if settings.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
    try:
        img = Image.open(io.BytesIO(data))
        text = pytesseract.image_to_string(img, lang=settings.ocr_lang)
    except Exception as e:
        raise ParseError(f"OCR 识别失败: {e}") from e
    text = text.strip()
    if not text:
        raise ParseError("图片扫描件 OCR 后无有效文字（可能清晰度不足或非合同内容）")
    return text


# ---------- 结构化提取 ----------

BASIC_PATTERNS = {
    "contract_title": [r"合同名称[：:]\s*(.+)"],
    "contract_no": [r"合同编号[：:]\s*([A-Za-z0-9\-/]+)"],
    "party_a": [r"甲方[（(]?(?:签约主体|采购方)?[）)?]?[：:]\s*([^\n，,。;；]{4,40})"],
    "party_b": [r"乙方[（(]?(?:供应商|服务方)?[）)?]?[：:]\s*([^\n，,。;；]{4,40})"],
    "amount": [r"(?:合同总?金额|总价)[^0-9]{0,6}([0-9,，]+(?:\.[0-9]+)?)\s*万?元"],
    "currency": [r"(?:币种|货币)[：:]\s*(\S{1,10})"],
    "effective_date": [r"(?:生效日期|生效时间|自)\s*[:：]?\s*([0-9]{4}年?[0-9]{1,2}月[0-9]{1,2}日)"],
    "expire_date": [r"(?:到期日期|终止日期|至)\s*[:：]?\s*([0-9]{4}年?[0-9]{1,2}月[0-9]{1,2}日)"],
}

CLAUSE_KEYWORDS = {
    "payment_clause": ["付款", "支付", "预付", "尾款", "结算"],
    "delivery_clause": ["交付", "交货", "供货", "工期"],
    "acceptance_clause": ["验收", "检验", "合格标准"],
    "breach_clause": ["违约", "赔偿", "责任"],
    "confidential_clause": ["保密", "机密", "披露"],
    "data_clause": ["个人信息", "数据安全", "数据处理"],
    "ip_clause": ["知识产权", "著作权", "专利", "成果归属"],
    "dispute_clause": ["争议", "纠纷", "管辖", "仲裁", "诉讼"],
}


def extract_structured(text: str) -> dict:
    """正则兜底提取基本信息与八类条款定位（LLM 增强在 services 层叠加）。"""
    basic = {}
    for field, patterns in BASIC_PATTERNS.items():
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                basic[field] = {"value": m.group(1).strip(),
                                "pos": m.start(), "status": "ok"}
                break
        else:
            basic[field] = {"value": None, "pos": None, "status": "missing"}

    clauses = {}
    for name, keywords in CLAUSE_KEYWORDS.items():
        found = None
        for kw in keywords:
            idx = text.find(kw)
            if idx >= 0:
                snippet = text[idx:idx + 160].replace("\n", " ")
                found = {"keywords_hit": kw, "snippet": snippet,
                         "pos": idx, "status": "present"}
                break
        clauses[name] = found or {"status": "absent"}

    return {"basic_info": basic, "clauses": clauses}


def safe_json_loads(raw: str | None, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default
