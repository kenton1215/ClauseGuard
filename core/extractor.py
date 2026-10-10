# -*- coding: utf-8 -*-
"""条款抽取与实体识别层（需求 R3/R4/R5）。

双通道设计：
1. 正则通道：确定性抽取，置信度高（0.90+），永远可用；
2. LLM 通道：语义补全，采用"头部优先 + 迭代传递"——将已识别要素摘要注入
   下一片段的提示词，防止长合同要素丢失（需求 R3）。

每个事实标准化为六元组：(受监管实体, 审计属性, 归一化值, 文档范围, 源片段, 置信度)。
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from core.doc_parser import ParsedDoc

# ---------------------------------------------------------------- 工具函数

CN_D = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9}
CN_U = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}

CN_CAP = {"壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5, "陆": 6,
          "柒": 7, "捌": 8, "玖": 9, "零": 0, "整": 0, "正": 0}
CN_CAP_U = {"拾": 10, "佰": 100, "仟": 1000, "万": 10000, "亿": 100000000,
            "圆": 1, "元": 1, "角": 0.1, "分": 0.01}


def cn_num_to_int(s: str) -> int:
    """中文小写数字转 int，'一千二百六十' -> 1260。"""
    total, section, num = 0, 0, 0
    for ch in s:
        if ch in CN_D:
            num = CN_D[ch]
        elif ch in CN_U:
            unit = CN_U[ch]
            if unit >= 10000:
                section = (section + num) * unit
                total += section
                section, num = 0, 0
            else:
                section += (num if num else 1) * unit
                num = 0
    return total + section + num


def cn_upper_to_float(s: str) -> float | None:
    """大写金额转数值：'壹佰贰拾万叁仟元整' -> 1203000.0。失败返回 None。"""
    s = re.sub(r"[^" + "".join(list(CN_CAP) + list(CN_CAP_U)) + "]", "", s)
    if not s:
        return None
    total, section, num = 0.0, 0.0, 0.0
    try:
        for ch in s:
            if ch in CN_CAP:
                num = float(CN_CAP[ch])
            elif ch in CN_CAP_U:
                unit = CN_CAP_U[ch]
                if ch in ("圆", "元"):
                    section += num
                    total += section
                    section, num = 0.0, 0.0
                elif ch in ("角", "分"):
                    total += (num if num else 0.0) * unit
                    section, num = 0.0, 0.0
                elif unit >= 10000:
                    section = (section + num) * unit
                    total += section
                    section, num = 0.0, 0.0
                else:
                    section += (num if num else 1.0) * unit
                    num = 0.0
        if num or section:
            total += section + num
        return round(total, 2) if total else None
    except (TypeError, ValueError):
        return None


def parse_date(s: str) -> str | None:
    """日期归一化为 ISO：2024年3月1日 / 2024-03-01 / 2024/3/1 -> 2024-03-01。"""
    m = re.search(r"(\d{4})\s*[年./\-]\s*(\d{1,2})\s*[月./\-]\s*(\d{1,2})\s*日?", s)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_amount_value(raw: str) -> float | None:
    """金额原文转"元"数值：'人民币1,200,000元' / '120万元' / '3.5亿元'。"""
    s = raw.replace(",", "").replace("，", "").replace(" ", "")
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(亿|万)?\s*(元|美元|港币|欧元)?", s)
    if not m:
        return None
    try:
        v = float(m.group(1))
        if m.group(2) == "亿":
            v *= 100000000
        elif m.group(2) == "万":
            v *= 10000
        return v
    except ValueError:
        return None


CREDIT_CODE_RE = re.compile(r"(?<![0-9A-Z])[0-9A-HJ-NPQRTUWXY]{18}(?![0-9A-Z])")
# 注意：DATE_RE 嵌入其他正则时需保持组号稳定，故使用非捕获组
DATE_RE = re.compile(r"(?:\d{4})\s*[年./\-]\s*(?:\d{1,2})\s*[月./\-]\s*(?:\d{1,2})\s*日?")
PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
CN_PERCENT_RE = re.compile(r"百分之([零〇一二两三四五六七八九十]+)")

COMPANY_RE = re.compile(
    r"(?:^|\n|[。；])\s*(?:甲方|乙方|买方|卖方|供方|需方|委托方|受托方|采购方|服务方|发包人|承包人|出租方|承租方)"
    r"(?:(?:（[^）]{0,6}）|\([^)]{0,6}\)))?[:：]?\s*"   # 支持"甲方（需方）："形式
    r"([一-鿿0-9A-Za-z（）()·\-.、]{2,40}"
    r"(?:股份有限公司|有限责任公司|有限公司|集团|研究院|学校|大学|医院|事务所|中心|工厂|银行|合作社|站|部))")

# 名称黑名单（动作性强，无论有无冒号均视为句中噪声）
NAME_HARD_STOPWORDS = ("负责", "运输", "协调", "提供", "按照", "约定", "通知", "到达",
                       "包括", "相关", "保证", "承担", "确保", "支付", "验收", "交付",
                       "签订", "签署", "安装", "调试", "培训", "维护", "自提", "放行")
# 行业词（合法公司名可能包含，仅"无冒号"场景视为噪声）
NAME_SOFT_STOPWORDS = ("采购", "销售", "生产", "测试")
# 无冒号时名称不得以介词开头（"甲方到乙方工厂"类噪声）
NAME_LEADING_FORBIDDEN = "到在向从由于给将需应"

# ---------------------------------------------------------------- 六元组

def fact(entity: str, attribute: str, value, scope: str, chunk: int, confidence: float) -> dict:
    return {"entity": entity, "attribute": attribute, "value": value,
            "scope": scope, "chunk": chunk, "confidence": confidence}


# ---------------------------------------------------------------- 正则通道

def extract_facts_regex(doc: ParsedDoc) -> dict:
    """确定性正则抽取，返回 facts 字典 + items 六元组列表。"""
    text = doc.text
    facts: dict = {}
    items: list[dict] = []

    def put(key, value, conf=0.95):
        facts[key] = value
        items.append(fact("合同", key, value, "全文", 0, conf))

    # ---- 主体信息
    parties = {}
    for m in COMPANY_RE.finditer(text):
        name = re.sub(r"[\s　]+", "", m.group(1)).strip("：:()（） ")
        if len(name) < 4 or len(name) > 60:
            continue
        if any(w in name for w in NAME_HARD_STOPWORDS):
            continue  # 动词黑名单：句子中间的 OCR 噪声
        has_colon = "：" in m.group(0) or ":" in m.group(0)
        if any(w in name for w in NAME_SOFT_STOPWORDS) and not has_colon:
            continue
        if not has_colon and (len(name) < 6 or (name and name[0] in NAME_LEADING_FORBIDDEN)):
            continue  # 无冒号的短名称/介词开头多为句中噪声
        role_raw = m.group(0)
        role = "甲方" if "甲" in role_raw else ("乙方" if "乙" in role_raw else role_raw[:3])
        if role not in parties:
            parties[role] = name
    for role in ("甲方", "乙方"):
        if role in parties:
            put(f"party_{'a' if role == '甲方' else 'b'}_name", parties[role], 0.92)

    # 统一社会信用代码：就近归属甲方/乙方（取代码前60字符内最近的角色标签）
    for m in CREDIT_CODE_RE.finditer(text):
        ctx = text[max(0, m.start() - 60):m.start()]
        pos_a, pos_b = ctx.rfind("甲方"), ctx.rfind("乙方")
        if pos_a == -1 and pos_b == -1:
            facts.setdefault("credit_codes", []).append(m.group(0))
        elif pos_a > pos_b:
            put("party_a_credit_code", m.group(0), 0.95)
        else:
            put("party_b_credit_code", m.group(0), 0.95)

    # 疑似信用代码格式异常（18位但含非法字符 I/O/S/V/Z 或全数字等）
    bad = re.findall(r"(?<![0-9A-Za-z])[0-9A-Za-z]{18}(?![0-9A-Za-z])", text)
    for code in bad:
        if CREDIT_CODE_RE.fullmatch(code) is None:
            put("credit_code_format_error", code, 0.9)
            break

    # ---- 合同编号
    m = re.search(r"(?:合同编号|合同号|Contract\s*No\.?)[:：]?\s*([A-Za-z0-9\-_/（）()]{3,40})", text)
    if m:
        put("contract_no", m.group(1).strip(), 0.92)

    # ---- 金额（总金额/合同价款 + 币种 + 含税 + 大写）
    m = re.search(r"(?:合同总金额|总金额|合同价款|总价款|合同总额|合计金额|合同金额)"
                  r"[:：为]?\s*[^\d]{0,30}((?:人民币|￥|¥|美元|USD)?\s*[0-9][0-9,，.\s]*"
                  r"(?:元|万元|亿元|美元)(?:\s*整)?)", text, flags=re.I)
    if m:
        raw = m.group(1).strip()
        val = parse_amount_value(raw)
        if val is not None:
            put("contract_amount", val, 0.9)
            put("contract_amount_raw", raw)
            currency = "人民币" if any(k in raw for k in ("人民币", "￥", "¥", "元")) else ("美元" if "美元" in raw else "未识别")
            put("currency", currency, 0.9)
    # 含税
    if re.search(r"(含税|含增值税|含(?:13%|9%|6%|3%)增值税)", text):
        put("tax_inclusive", True, 0.9)
    elif re.search(r"(不含税|未含税|除税)", text):
        put("tax_inclusive", False, 0.9)
    # 大写金额
    m = re.search(r"(?:大写|人民币大写)?[:：]?\s*([壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元角分零整正]{6,})", text)
    if m:
        upper = cn_upper_to_float(m.group(1))
        if upper is not None:
            put("amount_upper", upper, 0.95)

    # ---- 预付款（双向语序：'预付款比例为X%' 或 '支付总额的X%作为预付款'）
    # 40 字符窗口内禁止跨句号/分号，避免误抽后文比例
    m = re.search(r"(?:预付款|首付款|首期款|预付款比例)[^0-9%％。；\n]{0,40}(?:为|按|比例)?\s*"
                  r"(?:百分之)?([0-9]+(?:\.[0-9]+)?)\s*[%％]", text)
    if not m:
        m = re.search(r"(?:合同总额的|合同总价的|总额的|总价的|合同款的)?\s*"
                      r"(?:百分之)?([0-9]+(?:\.[0-9]+)?)\s*[%％]"
                      r"[^。；\n]{0,25}?(?:作为|为)?\s*(?:预付款|首付款|预付)", text)
    if m:
        put("prepayment_ratio", float(m.group(1)), 0.9)
    else:
        m = re.search(r"(?:预付款|首付款)[^0-9]{0,30}([0-9][0-9,，.\s]*(?:元|万元))", text)
        if m:
            amt = parse_amount_value(m.group(1))
            total = facts.get("contract_amount")
            if amt and total:
                put("prepayment_amount", amt, 0.88)
                put("prepayment_ratio", round(amt / total * 100, 1), 0.85)
            elif amt:
                put("prepayment_amount", amt, 0.88)

    # ---- 付款期限（天）（优先'验收后X日内支付'语序，再'付款期限为X日'）
    m = re.search(r"(?:验收合格后|验收后|到货后|提货后|收到发票后|交付后|结算后)"
                  r"[^0-9]{0,15}([0-9]+)\s*[个]?(?:工作日|日|天)"
                  r"[^。；\n]{0,25}?(?:内|之内)?\s*(?:支付|付款|结算)", text)
    if not m:
        m = re.search(r"(?:付款期限|支付期限|账期)[^0-9]{0,20}([0-9]+)\s*[个]?(?:工作日|日|天)", text)
    if not m:
        m = re.search(r"(?:付款|支付|结算)[^。；\n]{0,60}?(?:在|于|收到.{0,10}后|货到后|发票后)"
                      r"[^0-9]{0,20}([0-9]+)\s*[个]?(?:工作日|日|天)", text)
    if m:
        put("payment_days", float(m.group(1)), 0.85)

    # ---- 质保期（月）
    m = re.search(r"(?:质保期|保修期|保修期限|质保期限|质量保证期)[^0-9]{0,20}"
                  r"([0-9]+(?:\.[0-9]+)?)\s*(个)?(?:月|年)(?!度)", text)
    if m:
        v = float(m.group(1))
        if "年" in m.group(0):
            v *= 12
        put("warranty_months", v, 0.9)

    # ---- 违约金（双向语序：'违约金为X%' 或 '按…的X%支付违约金'）
    m = re.search(r"(?:违约金|逾期付款违约金)[^0-9%％。；\n]{0,50}?(?:为|按|以)?\s*(?:每日)?"
                  r"(?:百分之)?([0-9]+(?:\.[0-9]+)?)\s*[%％]", text)
    if not m:
        m = re.search(r"(?:按|以)?\s*(?:合同总额的|合同总价的|未付款项的|未付金额的|总价款的)?\s*"
                      r"(?:百分之)?([0-9]+(?:\.[0-9]+)?)\s*[%％]"
                      r"[^。；\n]{0,30}?(?:支付|计收|收取)?\s*违约金", text)
    if m:
        put("penalty_ratio", float(m.group(1)), 0.88)
    else:
        m = re.search(r"每日\s*(?:按|以)?\s*万分之([零〇一二两三四五六七八九十]+)", text)
        if m:
            v = cn_num_to_int(m.group(1))
            put("penalty_daily_permyriad", v, 0.88)  # 每日万分之X → 年化约 X*3.65%

    # ---- 日期：签订/生效/履行起止
    m = re.search(r"(?:签订日期|签署日期|签订时间|合同签订日期)[:：]?\s*(" + DATE_RE.pattern + ")", text)
    if m:
        put("signed_date", parse_date(m.group(1)), 0.9)
    m = re.search(r"(?:生效日期|生效时间)[:：]?\s*(" + DATE_RE.pattern + ")", text)
    if m:
        put("effective_date", parse_date(m.group(1)), 0.9)
    else:
        m = re.search(r"本合同自\s*(" + DATE_RE.pattern + r")起生效", text)
        if m:
            put("effective_date", parse_date(m.group(1)), 0.85)
    m = re.search(r"(?:履行(?:起始)?日期|履行开始|起始日期)[:：]?\s*(" + DATE_RE.pattern + ")", text)
    if m:
        put("performance_start", parse_date(m.group(1)), 0.88)
    m = re.search(r"(?:履行终止日期|履行截止|终止日期|结束日期)[:：]?\s*(" + DATE_RE.pattern + ")", text)
    if m:
        put("performance_end", parse_date(m.group(1)), 0.88)
    m = re.search(r"(?:有效期|合同期限)[为：:自]?\s*(" + DATE_RE.pattern + r")\s*(?:起|开始)?\s*(?:至|到|-|—)\s*(" + DATE_RE.pattern + ")", text)
    if m:
        put("performance_start", parse_date(m.group(1)), 0.9)
        put("performance_end", parse_date(m.group(2)), 0.9)

    # ---- 争议解决
    if re.search(r"仲裁委员会", text):
        put("dispute_mode", "仲裁", 0.9)
        m = re.search(r"(?:提交|向|申请)([一-鿿]{2,20}仲裁委员会)", text)
        if m:
            put("arbitration_body", m.group(1), 0.88)
    elif re.search(r"(人民法院|提起诉讼|诉讼)", text):
        put("dispute_mode", "诉讼", 0.9)
        m = re.search(r"(?:向|由)([一-鿿]{0,15}(?:人民法院))", text)
        if m:
            put("jurisdiction_court", m.group(1), 0.85)
    else:
        put("dispute_mode", None, 0.95)  # 缺失（高置信：全文未检出）

    # ---- 管辖地（前后各看30/50字符，判断对谁有利）
    m = re.search(r"[^。；\n]{0,30}(?:管辖|管辖地|管辖法院)[^。；\n]{0,50}", text)
    if m:
        seg = m.group(0)
        if "甲方" in seg:
            put("jurisdiction_favor", "甲方", 0.8)
        elif "乙方" in seg:
            put("jurisdiction_favor", "乙方", 0.8)
        elif "签订地" in seg:
            put("jurisdiction_favor", "合同签订地", 0.8)
        elif "履行地" in seg:
            put("jurisdiction_favor", "合同履行地", 0.8)

    # ---- 保密
    if re.search(r"保密", text):
        put("has_secrecy_clause", True, 0.85)
        m = re.search(r"(?:保密期|保密期限|保密义务)[^。；\n]{0,60}?(?:为|至|持续)?\s*"
                      r"(永久|长期|[0-9]+\s*年|合同(?:终止|解除)后\s*[0-9]+\s*年)", text)
        if not m:
            # 反向语序：'永久/长期…保密' 或 '…年…保密'
            m = re.search(r"(永久|长期)[^。；\n]{0,20}(?:保密|不得泄露|不得披露)", text)
        if m:
            g = m.group(1)
            if "永久" in g or "长期" in g:
                put("secrecy_years", 999, 0.85)
            else:
                mm = re.search(r"([0-9]+)", g)
                if mm:
                    put("secrecy_years", float(mm.group(1)), 0.85)
    else:
        put("has_secrecy_clause", False, 0.9)

    # ---- 知识产权归属
    if re.search(r"知识产权", text):
        put("has_ip_clause", True, 0.85)
        m = re.search(r"知识产权[^。；\n]{0,40}?(?:归|属于)(甲方|乙方|双方|受托方|委托方)", text)
        if m:
            put("ip_ownership", m.group(1), 0.88)

    # ---- 不可抗力
    if re.search(r"不可抗力", text):
        put("has_force_majeure", True, 0.9)
        if re.search(r"不可抗力[^。；\n]{0,80}(?:解除|终止|免责|通知)", text):
            put("force_majeure_consequence", True, 0.85)
    else:
        put("has_force_majeure", False, 0.9)

    # ---- 解除条件 / 自动续约 / 送达
    if re.search(r"(解除合同|合同解除)", text):
        put("has_termination_clause", True, 0.9)
    else:
        put("has_termination_clause", False, 0.85)
    if re.search(r"自动(?:续|延)(?:期|签|展)", text):
        put("auto_renewal", True, 0.9)
    if re.search(r"(送达地址|通知地址|文书送达)", text):
        put("has_notice_clause", True, 0.9)
    else:
        put("has_notice_clause", False, 0.85)

    # ---- 履约期天数（倒签与超长期检测用）
    if facts.get("performance_start") and facts.get("performance_end"):
        try:
            d1 = datetime.strptime(facts["performance_start"], "%Y-%m-%d")
            d2 = datetime.strptime(facts["performance_end"], "%Y-%m-%d")
            put("contract_days", (d2 - d1).days, 0.88)
        except ValueError:
            pass

    return {"facts": facts, "items": items}


# ---------------------------------------------------------------- LLM 通道

EXTRACT_PROMPT = """你是企业合同条款抽取引擎。请从【当前片段文本】中抽取结构化要素，输出严格 JSON。
抽取字段（能抽多少抽多少，抽取不到的字段不要输出）：
party_a_name 甲方名称, party_a_credit_code 甲方统一社会信用代码,
party_b_name 乙方名称, party_b_credit_code 乙方统一社会信用代码,
contract_no 合同编号, contract_name 合同名称,
contract_amount 合同总金额（单位：元，数值）, currency 币种,
tax_inclusive 是否含税(true/false), prepayment_ratio 预付款比例(数值，如40表示40%),
payment_days 付款期限(天), warranty_months 质保期(月),
penalty_ratio 违约金比例(数值%), signed_date 签订日期(YYYY-MM-DD),
effective_date 生效日期(YYYY-MM-DD), performance_start 履行起始日期(YYYY-MM-DD),
performance_end 履行终止日期(YYYY-MM-DD), dispute_mode 争议解决方式(诉讼/仲裁),
arbitration_body 仲裁机构, jurisdiction_court 管辖法院, jurisdiction_favor 管辖地对谁有利(甲方/乙方),
secrecy_years 保密期限(年), ip_ownership 知识产权归属(甲方/乙方/双方),
has_force_majeure 是否有不可抗力条款(true/false),
has_termination_clause 是否有解除条款(true/false),
auto_renewal 是否自动续约(true/false),
contract_subject 标的名{称/服务内容}, quantity 数量, specification 规格/标准,
payment_nodes 付款节点(文字描述)

规则：
1. 【已识别要素摘要】是前序片段已抽到的要素，若当前片段信息与之冲突，在 JSON 中输出 conflicts 数组说明；
2. 日期统一 YYYY-MM-DD；比例输出纯数值；
3. 只输出 JSON，不要任何解释。"""


def extract_with_llm(doc: ParsedDoc, llm) -> dict:
    """头部优先 + 迭代传递：逐片段抽取，已识别要素注入下一片段。"""
    merged: dict = {}
    conflicts: list = []
    summary_lines = []
    chunks = doc.chunks[:llm.max_chunks]
    if not chunks:
        chunks = doc.chunks[:1]

    for i, chunk in enumerate(chunks):
        summary = "\n".join(summary_lines) if summary_lines else "（暂无）"
        user = (f"【已识别要素摘要（来自前序片段）】\n{summary}\n\n"
                f"【当前片段文本】\n{chunk.text[:4000]}\n\n"
                f"【任务】请抽取要素并输出 JSON。")
        result = llm.chat_json("你是企业合同条款抽取引擎，只输出JSON。", user)
        if not isinstance(result, dict):
            continue
        for key, val in result.items():
            if key == "conflicts":
                if isinstance(val, list):
                    conflicts.extend(val)
                continue
            if val in (None, "", [], {}):
                continue
            if key not in merged:
                merged[key] = val
                summary_lines.append(f"- {key}: {val}")
    merged["_llm_conflicts"] = conflicts
    return merged


def merge_facts(regex_out: dict, llm_out: dict | None) -> dict:
    """合并正则与 LLM 抽取结果：正则置信度高者优先，LLM 补全缺失字段。"""
    facts = dict(regex_out["facts"])
    items = list(regex_out["items"])
    if llm_out:
        conflicts = llm_out.pop("_llm_conflicts", []) or []
        for key, val in llm_out.items():
            if val is None or val == "":
                continue
            if key in facts and facts[key] not in (None, ""):
                if str(facts[key]) != str(val):
                    items.append(fact("合同", key, f"正则:{facts[key]} / LLM:{val}",
                                      "全文", 0, 0.7))  # 冲突标记，置信度降级
                continue
            facts[key] = val
            items.append(fact("合同", key, val, "全文", 0, 0.8))
        if conflicts:
            facts["_conflicts"] = conflicts
    return {"facts": facts, "items": items}


def extract_all(doc: ParsedDoc, llm=None) -> dict:
    regex_out = extract_facts_regex(doc)
    llm_out = extract_with_llm(doc, llm) if llm and llm.enabled else None
    return merge_facts(regex_out, llm_out)
