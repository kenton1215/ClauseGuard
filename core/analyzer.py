# -*- coding: utf-8 -*-
"""审核编排层：解析 → 抽取 → 规则引擎 → 供应商核验 → 多智能体审查 → 融合 → 评分。

评分模型（需求 R9，扣分制）：
    高风险项 -15 / 中风险项 -8 / 低风险项 -3，下限 0；
    存在刚性不通过项（hard_block）时，风险等级强制为"高风险"并禁止流转。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from core.doc_parser import ParsedDoc, parse_document
from core.extractor import extract_all
from core.rule_engine import RuleEngine, load_rules
from core.rag import LawIndex, ClauseLibrary, CaseLibrary
from core.risks import llm_agent_review, check_supplier_db


@dataclass
class ReviewResult:
    doc: ParsedDoc
    facts: dict = field(default_factory=dict)
    fact_items: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    score: int = 100
    risk_level: str = "低风险"
    hard_block: bool = False
    duration_ms: int = 0
    report_md: str = ""
    llm_used: bool = False
    meta: dict = field(default_factory=dict)


def score_findings(findings: list[dict], score_cfg: dict) -> tuple[int, str, bool]:
    """扣分制评分。设计原则：评分以确定性规则引擎为准，
    LLM 智能体（AGENT-前缀）的建议按 50% 权重计分，避免语义审查过严导致评分失真。"""
    score = 100
    hard = any(f.get("hard_block") for f in findings)
    for f in findings:
        level = f.get("level", "中")
        deduct = score_cfg.get(f"deduct_{'high' if level == '高' else 'mid' if level == '中' else 'low'}",
                               15 if level == "高" else 8 if level == "中" else 3)
        if str(f.get("rule_id", "")).startswith("AGENT-"):
            deduct = max(1, deduct // 2)  # LLM 建议减半计分
        score -= deduct
    score = max(0, min(100, score))
    bands = score_cfg
    if hard or score < bands.get("band_high", 40):
        level = "高风险"
    elif score < bands.get("band_mid", 60):
        level = "较高风险"
    elif score < bands.get("band_low", 80):
        level = "中风险"
    else:
        level = "低风险"
    return score, level, hard


def dedupe(findings: list[dict]) -> list[dict]:
    """按 (标题, 条款位置) 去重，保留置信度高者。"""
    seen: dict[tuple, dict] = {}
    for f in findings:
        key = (f.get("title", ""), f.get("location", ""))
        if key not in seen or f.get("confidence", 0) > seen[key].get("confidence", 0):
            seen[key] = f
    out = sorted(seen.values(), key=lambda f: -({"高": 3, "中": 2, "低": 1}.get(f.get("level"), 0)))
    return out


class ContractAnalyzer:
    def __init__(self, settings: dict, llm=None):
        self.settings = settings
        self.llm = llm
        self.rules = load_rules()
        self.law_index = LawIndex()
        self.clause_lib = ClauseLibrary()
        self.case_lib = CaseLibrary()

    def analyze(self, file_bytes: bytes, file_name: str, contract_type: str = "",
                commitment_warranty_months: int | None = None,
                progress=None) -> ReviewResult:
        t0 = time.time()

        def report(stage: str, frac: float):
            if progress:
                progress(stage, frac)

        report("解析文档结构与条款切片", 0.1)
        doc = parse_document(file_bytes, file_name)
        if not doc.text.strip():
            result = ReviewResult(doc=doc, report_md="")
            result.findings = [{
                "rule_id": "PARSE-01", "title": "无法解析文档内容",
                "category": "程序合规风险", "level": "高", "hard_block": True,
                "location": "全文", "evidence": "未能从文件中提取有效文本（可能是纯扫描件或加密文件）",
                "suggestion": "请提供可复制文本的 PDF/Word 文件，或检查文件是否加密。",
                "basis": "文档解析层", "law_refs": [], "confidence": 1.0,
            }]
            result.score, result.risk_level, result.hard_block = 0, "高风险", True
            result.duration_ms = int((time.time() - t0) * 1000)
            return result

        report("抽取关键条款要素", 0.3)
        extracted = extract_all(doc, self.llm)
        facts = extracted["facts"]

        report("执行风险规则引擎", 0.55)
        findings = self.rules.evaluate(doc, facts, contract_type)

        # 外部承诺质保期比对（达观案例：投标承诺 vs 合同约定）
        if commitment_warranty_months and isinstance(facts.get("warranty_months"), (int, float)):
            if facts["warranty_months"] < commitment_warranty_months:
                findings.append({
                    "rule_id": "R003b", "title": "质保期低于投标承诺",
                    "category": "技术规范风险", "level": "高", "hard_block": False,
                    "location": "质保条款",
                    "evidence": f"合同质保期 {facts['warranty_months']} 个月 < 投标承诺 {commitment_warranty_months} 个月",
                    "suggestion": "按投标承诺修改质保期，或补充澄清文件说明变更依据。",
                    "basis": "投标承诺与合同一致性检查", "law_refs": [], "confidence": 0.95,
                })

        report("供应商资信核验", 0.65)
        findings.extend(check_supplier_db(facts))

        report("多智能体协同审查（法律/商业/技术/资信）", 0.8)
        agent_findings = llm_agent_review(
            doc, facts, findings, self.llm, self.law_index, self.clause_lib, self.case_lib)
        findings.extend(agent_findings)

        report("融合评分与生成报告", 0.95)
        findings = dedupe(findings)
        score, level, hard = score_findings(findings, self.settings.get("score", {}))
        result = ReviewResult(doc=doc, facts=facts, fact_items=extracted["items"],
                              findings=findings, score=score, risk_level=level,
                              hard_block=hard, llm_used=bool(self.llm and self.llm.enabled))
        result.duration_ms = int((time.time() - t0) * 1000)
        result.meta = {
            "file_name": file_name, "file_size": len(file_bytes),
            "contract_type": contract_type, "contract_name": facts.get("contract_name", ""),
            "contract_no": facts.get("contract_no", ""), "party_a": facts.get("party_a_name", ""),
            "party_b": facts.get("party_b_name", ""),
            "total_amount": str(facts.get("contract_amount", "")),
            "score": score, "risk_level": level, "hard_block": hard,
            "duration_ms": result.duration_ms,
        }
        return result
