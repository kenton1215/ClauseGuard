# -*- coding: utf-8 -*-
"""PDF 审核报告生成（reportlab + 微软雅黑，中文排版）。

输入结构化审核数据（meta/facts/findings），输出 PDF bytes，
供 Streamlit download_button 直接下载。
"""
from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from core.report import KEY_FIELDS, _fmt_ref, fmt_value

LEVEL_COLOR = {"高": colors.HexColor("#C0392B"), "中": colors.HexColor("#D68910"),
               "低": colors.HexColor("#B7950B")}
LEVEL_BG = {"高": colors.HexColor("#FDEDEC"), "中": colors.HexColor("#FEF5E7"),
            "低": colors.HexColor("#FEF9E7")}

_FONTS_READY = False


def _ensure_fonts() -> None:
    """注册中文字体。查找顺序：
    1. 项目内置开源字体 assets/fonts/wqy-microhei.ttc（随仓库分发，Streamlit Cloud 可用）；
    2. Windows 系统字体（本地部署）；
    3. Linux 常见系统字体（自托管容器）。"""
    global _FONTS_READY
    if _FONTS_READY:
        return
    project_fonts = Path(__file__).resolve().parent.parent / "assets" / "fonts"
    candidates = [
        ("CN", [project_fonts / "wqy-microhei.ttc",
                Path("C:/Windows/Fonts/msyh.ttc"),
                Path("C:/Windows/Fonts/simsun.ttc"),
                Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")]),
        ("CN-Bold", [project_fonts / "wqy-microhei.ttc",
                     Path("C:/Windows/Fonts/msyhbd.ttc"),
                     Path("C:/Windows/Fonts/simhei.ttf"),
                     Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
                     Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")]),
    ]
    for name, paths in candidates:
        for path in paths:
            if path.exists():
                try:
                    pdfmetrics.registerFont(TTFont(name, str(path), subfontIndex=0))
                    break
                except Exception:  # noqa: BLE001
                    continue
    if "CN" in pdfmetrics.getRegisteredFontNames():
        _FONTS_READY = True
    # 无中文字体：使用 Helvetica（中文会乱码，但程序不崩溃）


def _styles():
    _ensure_fonts()
    font = "CN" if "CN" in pdfmetrics.getRegisteredFontNames() else "Helvetica"
    font_b = "CN-Bold" if "CN-Bold" in pdfmetrics.getRegisteredFontNames() else font
    return {
        "title": ParagraphStyle("title", fontName=font_b, fontSize=20, leading=26,
                                alignment=1, spaceAfter=4),
        "subtitle": ParagraphStyle("subtitle", fontName=font, fontSize=9, leading=13,
                                   textColor=colors.HexColor("#666666"), alignment=1, spaceAfter=10),
        "h2": ParagraphStyle("h2", fontName=font_b, fontSize=13, leading=18,
                             spaceBefore=10, spaceAfter=6,
                             textColor=colors.HexColor("#2C3E50")),
        "body": ParagraphStyle("body", fontName=font, fontSize=9.5, leading=14.5,
                               spaceAfter=2),
        "small": ParagraphStyle("small", fontName=font, fontSize=8, leading=11,
                                textColor=colors.HexColor("#666666")),
        "score": ParagraphStyle("score", fontName=font_b, fontSize=26, leading=32,
                                alignment=1),
        "score_label": ParagraphStyle("score_label", fontName=font, fontSize=9, leading=12,
                                      alignment=1, textColor=colors.HexColor("#666666")),
        "finding_title": ParagraphStyle("finding_title", fontName=font_b, fontSize=10.5,
                                        leading=15, spaceBefore=6, spaceAfter=2),
    }


def _para_style(styles: dict, color: colors.Color | None = None) -> ParagraphStyle:
    st = styles["body"]
    if color:
        st = ParagraphStyle("body_c", parent=st, textColor=color)
    return st


def build_pdf_bytes(meta: dict, facts: dict, findings: list[dict]) -> bytes:
    """生成审核报告 PDF，返回 bytes。meta 需含 score/risk_level/hard_block 等。"""
    styles = _styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title="合同智能预审报告", author="企业合同审核智能体")
    story = []

    # ---- 页眉
    story.append(Paragraph("合同智能预审报告", styles["title"]))
    story.append(Paragraph(
        f"合同编号：{fmt_value(meta.get('contract_no'))}　|　"
        f"合同类型：{fmt_value(meta.get('contract_type'))}　|　"
        f"文件：{meta.get('file_name', '')}　|　"
        f"审查时间：{meta.get('reviewed_at') or datetime.now().strftime('%Y-%m-%d %H:%M')}",
        styles["subtitle"]))

    # ---- 评分区
    level = meta.get("risk_level", "")
    lv_color = LEVEL_COLOR.get(level, colors.HexColor("#2C3E50"))
    score_table = Table(
        [[Paragraph(f"<font color='{'#C0392B' if meta.get('hard_block') else '#2C3E50'}'>"
                    f"{meta.get('score', '—')}</font>/100", styles["score"]),
          Paragraph(level, ParagraphStyle("lv", parent=styles["score"],
                                          fontSize=18, textColor=lv_color))],
         [Paragraph("综合评分", styles["score_label"]),
          Paragraph("风险等级", styles["score_label"])]],
        colWidths=[80 * mm, 80 * mm])
    score_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#BDC3C7")),
        ("BACKGROUND", (0, 0), (-1, 0), LEVEL_BG.get(level, colors.white)),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(score_table)
    if meta.get("hard_block"):
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            "<font color='#C0392B'><b>⛔ 存在刚性不通过项，禁止合同流转，必须人工处理</b></font>",
            styles["body"]))
    story.append(Spacer(1, 6))

    # ---- 一、风险批注
    story.append(Paragraph(f"一、风险批注（共 {len(findings)} 条）", styles["h2"]))
    if not findings:
        story.append(Paragraph("未发现明显风险项。建议人工复核签章完整性、附件清单与审批文件。",
                               styles["body"]))
    for i, f in enumerate(findings, 1):
        lv = f.get("level", "中")
        color = LEVEL_COLOR.get(lv, colors.black)
        hard = "　【刚性不通过】" if f.get("hard_block") else ""
        story.append(Paragraph(
            f"{i}. [{lv}] {f.get('title', '')}{hard}", styles["finding_title"]))
        rows = [
            ("类别", f.get("category", "通用")),
            ("条款位置", f.get("location", "未定位")),
            ("原文依据", f.get("evidence", "")),
            ("修改建议", f.get("suggestion", "")),
            ("依据来源", f.get("basis", "")),
        ]
        refs = f.get("law_refs") or []
        if isinstance(refs, str):
            try:
                refs = json.loads(refs)
            except json.JSONDecodeError:
                refs = []
        if refs:
            rows.append(("法条依据", "；".join(_fmt_ref(r) for r in refs)))
        if f.get("status") and f.get("status") != "待处理":
            rows.append(("处理状态", f.get("status", "")))
        data = []
        for label, value in rows:
            data.append([Paragraph(f"<b>{label}</b>", styles["body"]),
                         Paragraph(str(value), styles["body"])])
        t = Table(data, colWidths=[22 * mm, 136 * mm])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -2), 0.3, colors.HexColor("#EEEEEE")),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ]))
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), LEVEL_BG.get(lv, colors.white))]))
        story.append(t)

    # ---- 二、合同要素摘要
    story.append(Paragraph("二、合同要素摘要", styles["h2"]))
    rows = [[Paragraph("<b>要素</b>", styles["body"]), Paragraph("<b>识别结果</b>", styles["body"])]]
    for label, key in KEY_FIELDS:
        rows.append([Paragraph(label, styles["body"]), Paragraph(fmt_value(facts.get(key)), styles["body"])])
    # 两列布局：24 要素拆成左右两栏
    half = (len(rows) + 1) // 2
    left, right = rows[:half], rows[half:]
    while len(left) < len(right):
        left.append([Paragraph("", styles["body"]), Paragraph("", styles["body"])])
    table_data = []
    for l, r in zip(left, right):
        table_data.append(l + r)
    t = Table(table_data, colWidths=[38 * mm, 41 * mm, 38 * mm, 41 * mm])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D5DBDB")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F4F6F7")),
        ("BACKGROUND", (2, 0), (3, 0), colors.HexColor("#F4F6F7")),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(t)

    # ---- 三、总体意见
    story.append(Paragraph("三、总体意见", styles["h2"]))
    hard_count = sum(1 for f in findings if f.get("hard_block"))
    high = sum(1 for f in findings if f.get("level") == "高")
    mid = sum(1 for f in findings if f.get("level") == "中")
    if hard_count:
        opinion = (f"本合同存在 {hard_count} 项刚性不通过风险、{high} 项高风险、{mid} 项中风险。"
                   f"建议：暂停合同流转，由法务部门人工复核后按批注意见修改，修改完成后重新送审。")
    elif (meta.get("score") or 100) < 80:
        opinion = (f"本合同存在 {high} 项高风险、{mid} 项中风险。"
                   f"建议：由业务部门确认风险可控性，法务复核修改意见后签署。")
    elif findings:
        opinion = (f"本合同总体风险可控（{high} 项高风险、{mid} 项中风险）。"
                   f"建议：逐条评估批注，柔性提示项可确认后流转。")
    else:
        opinion = "本合同未发现明显风险条款，建议完成签章核对、附件清单与授权审批后流转。"
    story.append(Paragraph(opinion, styles["body"]))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "本报告由企业合同审核智能体自动生成，仅供参考，最终决策以法务人工审查意见为准。",
        styles["small"]))

    doc.build(story)
    return buffer.getvalue()
