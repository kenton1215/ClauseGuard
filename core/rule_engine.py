# -*- coding: utf-8 -*-
"""规则引擎层（需求 R6）：确定性逻辑，数据驱动，不硬编码。

规则从 knowledge/rules/risk_rules.json 加载，支持以下 logic_type：
  ratio_gt      facts[key] > value          （比例类：预付款、违约金）
  days_gt       facts[key] > value          （期限类：付款期限、履约期）
  value_lt      facts[key] < value          （质保期低于阈值）
  missing       facts[key] 缺失/None/False   （缺争议解决、缺不可抗力…）
  text_absent   全文不含指定关键词           （缺签章区等）
  text_match    全文匹配正则                 （无限责任、倒签、自动续约…）
  date_after    facts[a] > facts[b]          （签订晚于履约起始=倒签）
  amount_mismatch  大写金额 != 小写金额      （大小写不一致）
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from core.config import KNOWLEDGE_DIR
from core.doc_parser import ParsedDoc

RULES_PATH = KNOWLEDGE_DIR / "rules" / "risk_rules.json"


class RuleEngine:
    def __init__(self, rules: list[dict]):
        self.rules = rules

    @classmethod
    def from_file(cls, path: Path | None = None) -> "RuleEngine":
        p = path or RULES_PATH
        if p.exists():
            try:
                return cls(json.loads(p.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                return cls([])
        return cls([])

    # ------------------------------------------------------------ 求值

    def evaluate(self, doc: ParsedDoc, facts: dict, contract_type: str = "") -> list[dict]:
        findings: list[dict] = []
        for rule in self.rules:
            if not rule.get("enabled", True):
                continue
            if not self._type_applies(rule, contract_type):
                continue  # 合同类型过滤：该规则不适用于此类型
            hit, evidence = self._check(rule, doc, facts)
            if hit:
                findings.append(self._build(rule, doc, facts, evidence, contract_type))
        return findings

    @staticmethod
    def _type_applies(rule: dict, contract_type: str) -> bool:
        """合同类型感知：contract_types=仅这些类型触发；exclude_types=这些类型不触发。"""
        ct = contract_type or ""
        if rule.get("contract_types") and ct not in rule["contract_types"]:
            return False
        if rule.get("exclude_types") and ct in rule["exclude_types"]:
            return False
        return True

    def _check(self, rule: dict, doc: ParsedDoc, facts: dict) -> tuple[bool, str]:
        logic = rule.get("logic_type", "")
        key = rule.get("fact_key", "")
        value = rule.get("value")
        text = doc.text

        try:
            if logic == "ratio_gt":
                v = facts.get(key)
                return (isinstance(v, (int, float)) and v > value), f"{key}={v}"
            if logic == "ratio_lt":
                v = facts.get(key)
                return (isinstance(v, (int, float)) and v < value), f"{key}={v}"
            if logic == "days_gt":
                v = facts.get(key)
                return (isinstance(v, (int, float)) and v > value), f"{key}={v}"
            if logic == "value_lt":
                v = facts.get(key)
                return (isinstance(v, (int, float)) and v < value), f"{key}={v}"
            if logic == "missing":
                v = facts.get(key)
                return v in (None, "", False, [], {}), f"{key}缺失"
            if logic == "present":
                v = facts.get(key)
                return v not in (None, "", False, [], {}), f"{key}={v}"
            if logic == "text_absent":
                # 语义：所有关键词均未检出才触发（任一命中即视为条款存在）
                keywords = rule.get("keywords", [])
                absent = [k for k in keywords if k not in text]
                return len(absent) == len(keywords) and bool(keywords), "全文未检出"
            if logic == "text_match":
                pattern = rule.get("pattern", "")
                if not pattern:
                    return False, ""
                m = re.search(pattern, text, flags=re.S)
                if m:
                    return True, self._snippet(text, m.start())
                return False, ""
            if logic == "date_after":
                a, b = facts.get(key), facts.get(rule.get("compare_key", ""))
                if not a or not b:
                    return False, ""
                from datetime import datetime as dt
                da = dt.strptime(str(a), "%Y-%m-%d")
                db = dt.strptime(str(b), "%Y-%m-%d")
                return da > db, f"{a} > {b}"
            if logic == "amount_mismatch":
                lower = facts.get("contract_amount")
                upper = facts.get("amount_upper")
                return (lower and upper and abs(lower - upper) > 0.01), \
                    f"小写{lower} vs 大写{upper}"
            if logic == "compare_lt":  # 投标承诺质保期比对（外部输入）
                v = facts.get(key)
                return (isinstance(v, (int, float)) and isinstance(value, (int, float))
                        and v < value), f"{key}={v} < 承诺{value}"
            if logic == "ip_ambiguous":
                # 提及知识产权但未明确归属（一般合同的降级提示）
                return (facts.get("has_ip_clause") and not facts.get("ip_ownership")), "提及知识产权但归属未明确"
            if logic == "fact_equals":
                # 事实值命中指定集合（如知识产权归乙方/受托方）
                values = rule.get("values", [])
                v = facts.get(key)
                return (v is not None and str(v) in values), f"{key}={v}"
        except (TypeError, ValueError):
            return False, ""
        return False, ""

    def _build(self, rule: dict, doc: ParsedDoc, facts: dict, evidence: str,
               contract_type: str = "") -> dict:
        keywords = rule.get("keywords", [])
        location = self._locate(doc, keywords)
        if not evidence:
            evidence = self._snippet_by_keywords(doc, keywords)
        # 风险等级可按合同类型细分（level_by_type），未命中细分则用默认等级
        level = rule.get("risk_level", "中")
        level_map = rule.get("level_by_type") or {}
        if contract_type and contract_type in level_map:
            level = level_map[contract_type]
        return {
            "rule_id": rule.get("id", ""),
            "title": rule.get("title", "风险项"),
            "category": rule.get("category", "通用"),
            "level": level,
            "hard_block": bool(rule.get("hard_block", False)),
            "location": location,
            "evidence": evidence or "未定位到明确证据段落",
            "suggestion": rule.get("suggestion", "请人工复核。"),
            "basis": rule.get("source", ""),
            "law_refs": rule.get("law_refs", []),
            "confidence": 0.9,
        }

    # ------------------------------------------------------------ 定位

    @staticmethod
    def _snippet(text: str, pos: int) -> str:
        # 从匹配位置所在行/段落的行首截取，避免证据片段偏移到无关条款
        line_start = max(text.rfind("\n", 0, pos), 0)
        if pos - line_start > 80:
            line_start = max(0, pos - 80)
        end = min(len(text), pos + 180)
        return re.sub(r"\s+", " ", text[line_start:end]).strip()

    @staticmethod
    def _snippet_by_keywords(doc: ParsedDoc, keywords: list[str]) -> str:
        for kw in keywords:
            idx = doc.text.find(kw)
            if idx != -1:
                return RuleEngine._snippet(doc.text, idx)
        return ""

    @staticmethod
    def _locate(doc: ParsedDoc, keywords: list[str]) -> str:
        from core.doc_parser import find_clause_for_pos
        for kw in keywords:
            idx = doc.text.find(kw)
            if idx != -1:
                clause = find_clause_for_pos(doc, idx)
                return f"{clause}" if clause else "全文"
        return "全文"


def load_rules() -> RuleEngine:
    return RuleEngine.from_file()
