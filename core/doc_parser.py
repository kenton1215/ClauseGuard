# -*- coding: utf-8 -*-
"""文档解析与切片层（需求 R1/R2）。

- DOCX：按文档流顺序提取段落与表格（保留版面顺序、合并单元格信息）；
- PDF：PyMuPDF 提取文本层，扫描件自动回退 OCR（延迟加载，避免启动开销）；
- 切片：条款边界识别（第X条/第X章/数字编号）+ 5000 字符窗口 + 300 字符重叠，
  保证不切断完整条款，支持"头部优先 + 迭代传递"上下文机制（需求 R3）。
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field


@dataclass
class Clause:
    no: str            # 条款编号文本，如 "第5条" / "8.2"
    start: int
    end: int
    text: str


@dataclass
class Chunk:
    seq: int
    text: str
    start: int
    end: int
    clause_refs: list[str] = field(default_factory=list)  # 所属条款编号


@dataclass
class ParsedDoc:
    text: str
    file_name: str
    has_text_layer: bool = True
    needs_ocr: bool = False
    tables: list = field(default_factory=list)     # [{index, rows, page}]
    clauses: list = field(default_factory=list)    # [Clause]
    chunks: list = field(default_factory=list)     # [Chunk]
    signature_area: str = ""                       # 签章区文本
    head_text: str = ""                            # 首部（主体信息区）

    def summary(self) -> dict:
        return {
            "file_name": self.file_name, "chars": len(self.text),
            "clauses": len(self.clauses), "chunks": len(self.chunks),
            "tables": len(self.tables), "needs_ocr": self.needs_ocr,
        }


CLAUSE_HEAD_RE = re.compile(
    r"^\s*(第[零〇一二两三四五六七八九十百千0-9０-９]+[条章编节]|\d{1,3}(?:\.\d{1,2}){1,3}\s*[、.．]|[一二三四五六七八九十]{1,3}\s*[、.])")
ARTICLE_RE = re.compile(r"第[零〇一二两三四五六七八九十百千0-9０-９]+条")

CHUNK_MAX = 5000
CHUNK_OVERLAP = 300


# ---------------------------------------------------------------- 文件解析

def extract_text_docx(file_bytes: bytes) -> tuple[str, list]:
    """按文档流顺序提取 docx 段落与表格，返回 (文本, 表格列表)。"""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(file_bytes))
    parts, tables = [], []
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            para = Paragraph(child, doc)
            parts.append(para.text)
        elif child.tag.endswith("}tbl"):
            table = Table(child, doc)
            rows = [[cell.text.strip().replace("\n", " ") for cell in row.cells] for row in table.rows]
            tables.append({"index": len(tables) + 1, "rows": rows})
            # 表格以紧凑形式并入正文，保证上下文完整
            for row in rows:
                parts.append(" | ".join(row))
    return "\n".join(parts), tables


def extract_text_pdf(file_bytes: bytes) -> tuple[str, bool, list]:
    """PyMuPDF 提取 PDF 文本层；无文本层返回空串并标记 needs_ocr。"""
    try:
        import pymupdf as fitz
    except ImportError:  # 兼容旧版本包名
        import fitz
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    pages, total_chars = [], 0
    for page in doc:
        text = page.get_text("text") or ""
        total_chars += len(text.strip())
        pages.append(text)
    doc.close()
    return "\n".join(pages), total_chars > 40, []


def ocr_pdf(file_bytes: bytes) -> str:
    """扫描件 OCR 回退（延迟加载 RapidOCR，逐页渲染识别）。"""
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
        from rapidocr_onnxruntime import RapidOCR
        engine = RapidOCR()
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        out = []
        for page in doc:
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            result, _ = engine(pix.tobytes("png"))
            if result:
                out.append("\n".join(item[1] for item in result if isinstance(item, (list, tuple)) and len(item) > 1))
        doc.close()
        return "\n".join(out)
    except Exception as e:  # noqa: BLE001
        print(f"[OCR] 识别失败: {e}")
        return ""


def extract_text_txt(file_bytes: bytes) -> str:
    for enc in ("utf-8", "gbk", "utf-16"):
        try:
            return file_bytes.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return file_bytes.decode("utf-8", errors="ignore")


def parse_document(file_bytes: bytes, file_name: str) -> ParsedDoc:
    name = file_name.lower()
    text, tables, has_layer, needs_ocr = "", [], True, False
    if name.endswith(".docx"):
        text, tables = extract_text_docx(file_bytes)
    elif name.endswith(".pdf"):
        text, has_layer, tables = extract_text_pdf(file_bytes)
        if not has_layer:
            needs_ocr = True
            text = ocr_pdf(file_bytes)
            has_layer = bool(text.strip())
    elif name.endswith((".txt", ".md", ".doc")):
        text = extract_text_txt(file_bytes)
    else:
        text = ""
    return build_parsed(text, file_name, tables, has_layer, needs_ocr)


# ---------------------------------------------------------------- 条款与切片

def build_parsed(text: str, file_name: str, tables: list,
                 has_layer: bool, needs_ocr: bool) -> ParsedDoc:
    doc = ParsedDoc(text=text, file_name=file_name, tables=tables,
                    has_text_layer=has_layer, needs_ocr=needs_ocr)
    if not text.strip():
        return doc

    # 条款边界索引
    clauses: list[Clause] = []
    for m in re.finditer(r"(?:^|\n)\s*((?:第[零〇一二两三四五六七八九十百千0-9０-９]+[条章]|\d{1,3}(?:\.\d{1,2}){1,3}))\s*[、.．]?", text):
        head = m.group(1).strip()
        body_start = m.end()
        nxt = re.search(r"(?:^|\n)\s*(?:第[零〇一二两三四五六七八九十百千0-9０-９]+[条章]|\d{1,3}(?:\.\d{1,2}){1,3})\s*[、.．]?", text[body_start:], flags=re.M)
        body_end = body_start + nxt.start() if nxt else min(len(text), body_start + 4000)
        if "章" in head and not head.endswith("条"):
            continue  # 章标题仅作章节标记，不单列
        clauses.append(Clause(no=head, start=m.start(), end=body_end,
                              text=text[body_start:body_end].strip()[:2000]))
    doc.clauses = clauses

    # 签章区：正文末尾包含签章关键词的区域
    tail = text[-3000:]
    sig_idx = max(tail.find("盖章"), tail.find("签章"), tail.find("授权代表"), tail.find("签字"))
    if sig_idx != -1:
        doc.signature_area = tail[max(0, sig_idx - 300):sig_idx + 400].strip()

    # 首部（主体信息区）：前 2500 字符
    doc.head_text = text[:2500]

    # 滑动窗口切片：沿条款边界切分，保证条款不被切断
    chunks: list[Chunk] = []
    pos, seq = 0, 1
    boundaries = sorted({c.start for c in clauses} | {0, len(text)})
    while pos < len(text):
        end = min(len(text), pos + CHUNK_MAX)
        # 若切片终点落在某条款中间，回退到该条款起点
        for b in boundaries:
            if pos < b < end:
                end = b
                break
        chunk = text[pos:end].strip()
        if chunk:
            refs = [c.no for c in clauses if pos <= c.start < end]
            chunks.append(Chunk(seq=seq, text=chunk, start=pos, end=end, clause_refs=refs))
            seq += 1
        if end >= len(text):
            break
        # 重叠：从上一片段末尾向前回退 300 字符，并落在条款边界上
        pos = max(pos, end - CHUNK_OVERLAP)
        if pos <= max(c.start for c in chunks[-1:]) and pos < end:
            pos = end
    doc.chunks = chunks
    return doc


def find_clause_for_pos(doc: ParsedDoc, position: int) -> str:
    """根据字符位置定位所属条款编号。"""
    best = ""
    for c in doc.clauses:
        if c.start <= position <= c.end:
            best = c.no
            break
    return best or "全文"
