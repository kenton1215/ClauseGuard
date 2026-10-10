# -*- coding: utf-8 -*-
"""审核报告生成（需求 R10）：参照达观案例格式，含风险批注/要素摘要/审查依据。"""
from __future__ import annotations

from datetime import datetime

from core.analyzer import ReviewResult

LEVEL_ICON = {"高": "🔴", "中": "🟠", "低": "🟡"}

# 要素摘要字段（Markdown 报告与 PDF 报告共用）
KEY_FIELDS = [
    ("甲方名称", "party_a_name"), ("甲方信用代码", "party_a_credit_code"),
    ("乙方名称", "party_b_name"), ("乙方信用代码", "party_b_credit_code"),
    ("合同总金额", "contract_amount"), ("币种", "currency"),
    ("含税", "tax_inclusive"), ("预付款比例", "prepayment_ratio"),
    ("付款期限(天)", "payment_days"), ("质保期(月)", "warranty_months"),
    ("违约金比例", "penalty_ratio"), ("签订日期", "signed_date"),
    ("生效日期", "effective_date"), ("履行起始", "performance_start"),
    ("履行终止", "performance_end"), ("争议解决方式", "dispute_mode"),
    ("管辖对谁有利", "jurisdiction_favor"), ("保密期限(年)", "secrecy_years"),
    ("知识产权归属", "ip_ownership"), ("不可抗力条款", "has_force_majeure"),
    ("解除条款", "has_termination_clause"), ("自动续约", "auto_renewal"),
    ("通知送达条款", "has_notice_clause"),
]


def _fmt_ref(r) -> str:
    """格式化法条引用，兼容两类结构：
    {'law': '民法典', 'article': 584, 'point': '...'} 与 {'law': '内控指引16号', 'point': '...'}。"""
    if isinstance(r, dict):
        law = r.get("law", "")
        point = r.get("point", "")
        article = r.get("article")
        if article:
            return f"《{law}》第{article}条（{point}）" if point else f"《{law}》第{article}条"
        return f"《{law}》（{point}）" if point else f"《{law}》"
    return str(r)


def fmt_value(v) -> str:
    if v is None or v == "":
        return "未识别"
    if isinstance(v, bool):
        return "是" if v else "否"
    return str(v)


def build_report(result: ReviewResult) -> str:
    f = result.facts
    doc = result.doc
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    hard_note = "⛔ 存在刚性不通过项，禁止合同流转，必须人工处理" if result.hard_block else \
        ("⚠️ 存在中高风险项，需人工复核确认" if result.score < 80 else "✅ 未发现刚性风险，可进入常规流转")

    lines = [
        "# 【合同智能预审报告】",
        "",
        f"- 合同编号：{fmt_value(f.get('contract_no'))}",
        f"- 合同类型：{result.meta.get('contract_type') or '未指定'}",
        f"- 审查时间：{now}",
        f"- 审查用时：{result.duration_ms / 1000:.1f} 秒（{doc.summary()['clauses']} 个条款 / {doc.summary()['chunks']} 个片段）",
        f"- 审查引擎：{'规则引擎 + LLM 多智能体' if result.llm_used else '规则引擎（LLM 未启用）'}",
        f"- **综合评分：{result.score}/100（{result.risk_level}）**",
        "",
        hard_note,
        "",
    ]

    # ---- 一、风险批注
    lines.append(f"## 一、风险批注（共 {len(result.findings)} 条）")
    lines.append("")
    if not result.findings:
        lines.append("未发现明显风险项。建议人工复核签章完整性、附件清单与审批文件。")
    for i, issue in enumerate(result.findings, 1):
        icon = LEVEL_ICON.get(issue.get("level"), "")
        hard = "【刚性不通过】" if issue.get("hard_block") else ""
        lines += [
            f"### [风险#{i}] {icon} {issue.get('title', '')} · {issue.get('level', '')} {hard}",
            f"- 类别：{issue.get('category', '通用')}",
            f"- 条款位置：{issue.get('location', '未定位')}",
            f"- 原文依据：{issue.get('evidence', '')}",
            f"- 修改建议：{issue.get('suggestion', '')}",
            f"- 依据来源：{issue.get('basis', '')}",
        ]
        refs = issue.get("law_refs") or []
        if refs:
            ref_str = "；".join(_fmt_ref(r) for r in refs)
            lines.append(f"- 法条依据：{ref_str}")
        if issue.get("confidence") and issue["confidence"] < 0.9:
            lines.append(f"- 置信度：{issue['confidence']:.2f}（建议人工复核）")
        lines.append("")

    # ---- 二、合同要素摘要
    lines += [
        "## 二、合同要素摘要",
        "",
        "| 要素 | 识别结果 | 置信度 |",
        "| --- | --- | --- |",
    ]
    conf_map = {item["attribute"]: item["confidence"] for item in result.fact_items}
    for label, key in KEY_FIELDS:
        v = f.get(key)
        conf = conf_map.get(key)
        conf_str = f"{conf:.2f}" if conf is not None else "-"
        lines.append(f"| {label} | {fmt_value(v)} | {conf_str} |")
    lines.append("")

    # ---- 三、审查依据清单
    lines += ["## 三、审查依据清单", ""]
    laws = sorted({f"《{r.get('law')}》" for issue in result.findings
                   for r in (issue.get("law_refs") or []) if isinstance(r, dict) and r.get("law")})
    if laws:
        lines.append("- 法律法规：" + "、".join(laws))
    sources = sorted({issue.get("basis", "") for issue in result.findings if issue.get("basis")})
    if sources:
        lines.append("- 制度与规则：" + "；".join(s for s in sources if s))
    from core.rule_engine import load_rules
    lines.append(f"- 风险规则库版本：knowledge/rules/risk_rules.json（{len(load_rules().rules)} 条可配置规则）")
    lines.append("- 标准条款库版本：knowledge/standards/clause_library.json")
    lines.append("")

    # ---- 四、总体意见
    lines += ["## 四、总体意见", ""]
    hard_count = sum(1 for i in result.findings if i.get("hard_block"))
    high = sum(1 for i in result.findings if i.get("level") == "高")
    mid = sum(1 for i in result.findings if i.get("level") == "中")
    if result.hard_block:
        lines.append(f"本合同存在 {hard_count} 项刚性不通过风险、{high} 项高风险、{mid} 项中风险。"
                     f"建议：暂停合同流转，由法务部门人工复核后按批注意见修改，修改完成后重新送审。")
    elif result.score < 80:
        lines.append(f"本合同存在 {high} 项高风险、{mid} 项中风险。建议：由业务部门确认风险可控性，"
                     f"法务复核修改意见后签署。")
    elif result.findings:
        lines.append(f"本合同总体风险可控（{high} 项高风险、{mid} 项中风险）。"
                     f"建议：逐条评估批注，柔性提示项可确认后流转。")
    else:
        lines.append("本合同未发现明显风险条款，建议完成签章核对、附件清单与授权审批后流转。")
    lines.append("")
    lines.append("> 本报告由合同审核智能体自动生成，仅供参考，最终决策以法务人工审查意见为准。")
    lines.append("")
    if result.facts.get("_conflicts"):
        lines.append(f"> ⚠️ 要素冲突提示：{result.facts['_conflicts']}")

    return "\n".join(lines)
