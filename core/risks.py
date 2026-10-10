# -*- coding: utf-8 -*-
"""语义风险检查 + 多智能体协同层（需求 R8）。

P0 实现采用"单模型多角色"拓扑（优化说明见 docs/方案优化说明.md）：
  第一级：条款分类（关键词路由，零 LLM 调用）
  第二级：4 个专业 Risk Agent（法律合规/商业条件/技术规范/供应商信用）
          按角色提示词 + RAG 检索结果审查对应类别条款
  第三级：Fusion Agent（汇总去重 + 跨域风险联动）
LLM 不可用时整体跳过，规则引擎通道仍完整可用。
"""
from __future__ import annotations

import json

from core.doc_parser import ParsedDoc
from core.rag import LawIndex, ClauseLibrary, CaseLibrary

AGENT_SPECS = [
    ("法律合规", ["违约责任", "争议解决", "管辖", "解除", "保密", "不可抗力", "生效", "签章", "通知", "格式条款", "无限责任", "仲裁"],
     "你是一名资深企业法务，专精《民法典》合同编与《民事诉讼法》。请审查【条款原文】，识别法律合规风险："
     "条款效力风险、权利义务失衡、违反法律强制性规定、争议解决机制缺陷等。"),
    ("商业条件", ["付款", "预付款", "价格", "价款", "发票", "续约", "期限", "质保金", "结算"],
     "你是一名企业采购与商务谈判专家。请审查【条款原文】，识别商业条件风险："
     "付款结构不合理、价格锁定风险、账期过长、商业条款模糊等。"),
    ("技术规范", ["验收", "质量标准", "技术", "知识产权", "专利", "著作权", "质保", "保修", "规格"],
     "你是一名技术合同与知识产权专家。请审查【条款原文】，识别技术规范风险："
     "验收标准缺失、知识产权归属不清、技术指标模糊、质保责任不清等。"),
    ("供应商信用", ["主体", "资质", "信用代码", "经营范围", "注册资本"],
     "你是一名供应商管理专家。请审查【条款原文】，识别主体与资信风险："
     "主体信息不完整、资质缺失、与供应商库记录不一致等。"),
]

CLAUSE_SPLIT_RE = r"(第[零〇一二两三四五六七八九十百千0-9]+条|\d{1,3}(?:\.\d{1,2}){1,3})"


def classify_clauses(doc: ParsedDoc) -> dict[str, list[str]]:
    """按关键词将条款文本路由到 4 个专业类别。"""
    buckets = {name: [] for name, _, _ in AGENT_SPECS}
    source = doc.clauses if doc.clauses else [type("C", (), {"text": doc.text[:3000]})()]
    for clause in source:
        text = clause.text.strip()
        if not text or len(text) < 10:
            continue
        for name, kws, _ in AGENT_SPECS:
            if any(kw in text for kw in kws):
                buckets[name].append(text)
                break
        else:
            buckets["法律合规"].append(text)  # 默认归法律合规审查
    return buckets


def _law_context(law_index: LawIndex, texts: list[str], top_k: int = 6) -> str:
    """RAG：为条款检索相关法条，拼成审查上下文。"""
    lines = []
    seen = set()
    for text in texts[:12]:
        for a in law_index.retrieve(text, top_k=2):
            key = (a["law"], a["no"])
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"- 《{a['law']}》第{a['no']}条：{a['text'][:150]}")
            if len(lines) >= top_k:
                break
        if len(lines) >= top_k:
            break
    return "\n".join(lines) if lines else "（未检索到直接相关法条）"


def llm_agent_review(doc: ParsedDoc, facts: dict, rule_findings: list[dict], llm,
                     law_index: LawIndex, clause_lib: ClauseLibrary,
                     case_lib: CaseLibrary) -> list[dict]:
    """多智能体协同审查：分类 → 4 专业 Agent → Fusion。LLM 不可用返回 []。"""
    if not llm or not llm.enabled:
        return []

    buckets = classify_clauses(doc)
    agent_outputs: list[dict] = []

    # 已识别事实摘要：告知各 Agent 规则引擎已确认的内容，避免重复挑刺
    fact_summary = "\n".join(
        f"- {k}: {v}" for k, v in facts.items()
        if not k.startswith("_") and v not in (None, "", False)) or "（规则引擎未识别到要素）"

    for agent_name, _, persona in AGENT_SPECS:
        clauses = buckets.get(agent_name, [])
        if not clauses:
            continue
        material = "\n\n".join(f"[条款{i}] {c[:600]}" for i, c in enumerate(clauses[:8], 1))
        law_ctx = _law_context(law_index, clauses)
        clause_ref = clause_lib.find(" ".join(clauses[:2]))
        clause_ctx = "\n".join(
            f"- 标准条款《{c.get('type', '')}》：{c.get('preferred_text', '')[:120]}" for c in clause_ref) or "（无匹配标准条款）"
        case_ctx = "\n".join(
            f"- 历史案例：{c.get('issue', '')}（{c.get('risk_level', '')}，处理：{c.get('final_decision', '')}）"
            for c in case_lib.items[:3]) or "（无相似案例）"

        user = (
            f"【审查角色】{agent_name}\n\n"
            f"【条款原文】\n{material}\n\n"
            f"【规则引擎已识别事实（这些内容条款已明确，不要重复报告）】\n{fact_summary}\n\n"
            f"【相关法条】\n{law_ctx}\n\n"
            f"【企业标准条款】\n{clause_ctx}\n\n"
            f"【历史案例参考】\n{case_ctx}\n\n"
            f"【任务】只报告条款原文中真实存在且可引用原文佐证的实质缺陷。输出 JSON："
            f"{{\"issues\":[{{\"clause\":\"条款位置\",\"title\":\"风险标题（不超过20字）\","
            f"\"level\":\"高/中/低\",\"quote\":\"条款原文摘录（缺陷所在句子，必填，不超过60字）\","
            f"\"suggestion\":\"具体修改建议（不超过100字）\","
            f"\"basis\":\"依据（法条/制度，不超过40字）\"}}]}}。\n"
            f"风险分级标准（严格遵守）：\n"
            f"- 高：条款存在法律效力风险、权利义务严重失衡、可能被认定无效（如无限责任、或裁或审、排除法定权利）；\n"
            f"- 中：存在明确的不利商业条件或合规缺陷（如违约责任单向约定、赔偿上限明显过宽、付款结构与验收脱钩）；\n"
            f"- 低：仅属完善性建议、不影响条款效力与核心权益（如补充响应时限、补充兜底表述）。\n"
            f"严格约束：1) 每角色最多报2条，只报最有价值的；2) 条款已明确约定的事项不得报告；"
            f"3) 完善性建议仅在确有价值时报'低'，宁缺毋滥；"
            f"4) quote 必须逐字摘自条款原文，无法摘录的不要报告；5) 无实质缺陷输出 {{\"issues\":[]}}。只输出 JSON。")
        result = llm.chat_json(persona, user)
        if isinstance(result, dict):
            for iss in result.get("issues", []):
                if not isinstance(iss, dict) or not iss.get("title"):
                    continue
                quote = (iss.get("quote") or "").strip()
                level = iss.get("level", "中") if iss.get("level") in ("高", "中", "低") else "中"
                conf = 0.8
                # 证据校验：quote 未摘录原文或与原文不符（忽略空白差异）→ 降置信、降级
                import re as _re
                norm_text = _re.sub(r"\s+", "", doc.text)
                norm_quote = _re.sub(r"\s+", "", quote)
                if not quote or len(norm_quote) < 6 or norm_quote[:8] not in norm_text:
                    conf = 0.6
                    level = "低" if level == "高" else level
                agent_outputs.append({
                    "rule_id": f"AGENT-{agent_name}",
                    "title": iss.get("title", "风险项"),
                    "category": f"{agent_name}风险",
                    "level": level,
                    "hard_block": False,
                    "location": iss.get("clause", "条款区"),
                    "evidence": quote or iss.get("basis", ""),
                    "suggestion": iss.get("suggestion", "请人工复核。"),
                    "basis": iss.get("basis", ""),
                    "law_refs": [],
                    "confidence": conf,
                    "source_agent": agent_name,
                })

    if not agent_outputs:
        return []

    # ---- Fusion：与规则引擎结果去重（标题相似视为同一风险）
    existing_titles = {f["title"] for f in rule_findings}
    fused = []
    for out in agent_outputs:
        if out["title"] in existing_titles:
            continue
        fused.append(out)

    # 跨域风险联动：供应商风险 + 高预付款 → 升级
    has_supplier_risk = any("供应商" in f.get("title", "") for f in fused + rule_findings)
    high_prepayment = facts.get("prepayment_ratio") and facts["prepayment_ratio"] > 30
    if has_supplier_risk and high_prepayment:
        fused.append({
            "rule_id": "FUSION-01", "title": "供应商风险与高预付款叠加",
            "category": "跨域联动风险", "level": "高", "hard_block": True,
            "location": "综合评估", "evidence": "供应商资信风险与预付款比例高于30%叠加，资金敞口放大",
            "suggestion": "综合风险升级：要求预付款保函+分期付款+供应商资信担保，或暂停签约。",
            "basis": "Fusion Agent 跨域风险关联规则", "law_refs": [], "confidence": 0.85,
        })
    return fused


def check_supplier_db(facts: dict) -> list[dict]:
    """供应商主数据库核验（需求 1.4，P0：本地库匹配）。"""
    from core.config import KNOWLEDGE_DIR
    import re as _re
    path = KNOWLEDGE_DIR / "supplier" / "supplier_db.json"
    if not path.exists():
        return []
    try:
        db = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    name = str(facts.get("party_b_name") or "").strip()
    if not name:
        return []
    findings = []
    for rec in db:
        rec_name = str(rec.get("name", ""))
        if rec_name and (rec_name == name or rec_name in name or name in rec_name):
            risk = rec.get("risk", {})
            flags = risk.get("flags", [])
            if flags:
                findings.append({
                    "rule_id": "SUP-01", "title": f"乙方命中供应商风险库（{len(flags)}项）",
                    "category": "履约能力风险", "level": "高",
                    "hard_block": any(f in ("失信被执行人", "经营异常", "吊销执照") for f in flags),
                    "location": "主体信息区",
                    "evidence": "；".join(f"{f}" for f in flags) + f"。风险等级：{risk.get('level', '未评级')}",
                    "suggestion": "核实风险状态是否已消除；未消除的暂停签约或要求提供担保/更换供应商。",
                    "basis": "供应商主数据库（企业信用信息公示系统/执行信息公开网数据）",
                    "law_refs": [], "confidence": 0.95,
                })
            break
    return findings
