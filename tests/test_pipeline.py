# -*- coding: utf-8 -*-
"""集成测试：抽取→规则→评分→数据库闭环（不依赖外部 API 与 Streamlit）。"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import db  # noqa: E402
from core.analyzer import ContractAnalyzer, score_findings  # noqa: E402
from core.config import load_settings  # noqa: E402
from core.doc_parser import parse_document  # noqa: E402
from core.extractor import cn_num_to_int, cn_upper_to_float, extract_facts_regex  # noqa: E402
from core.rule_engine import RuleEngine  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    return db


# ---------------------------------------------------------------- 基础工具

def test_cn_num():
    assert cn_num_to_int("一千二百六十") == 1260
    assert cn_num_to_int("三百五十九") == 359


def test_cn_upper_amount():
    assert cn_upper_to_float("壹佰贰拾万元整") == 1200000.0
    assert cn_upper_to_float("壹拾贰万叁仟肆佰伍拾陆元柒角捌分") == 123456.78
    assert cn_upper_to_float("叁仟万元") == 30000000.0


# ---------------------------------------------------------------- 抽取

RISKY_TEXT = """
甲方：示例采购集团股份有限公司
统一社会信用代码：91110000MA01AAAA01
乙方：示例科技有限公司
统一社会信用代码：91110108MA01DEM001
合同编号：HT-2025-001
第二条 合同总金额：人民币1280000元（大写：壹佰贰拾万零捌仟元整）。
第三条 合同签订后10日内，甲方支付合同总额的50%作为预付款。
验收合格后120个工作日内支付剩余全部款项。
第四条 质保期为验收合格后6个月。
第五条 违约金比例为合同总额的40%。
签订日期：2025年3月20日
第六条 本合同有效期自2025年2月10日起至2026年2月9日止。
合同期满双方未提出异议的自动续期一年。
签署区 甲方（盖章）： 乙方（盖章）：
"""


def test_extract_facts_regex():
    doc = parse_document(RISKY_TEXT.encode("utf-8"), "test.txt")
    out = extract_facts_regex(doc)
    f = out["facts"]
    assert f["party_a_name"] == "示例采购集团股份有限公司"
    assert f["party_b_name"] == "示例科技有限公司"
    assert f["party_b_credit_code"] == "91110108MA01DEM001"
    assert f["contract_amount"] == 1280000.0
    assert f["amount_upper"] == 1208000.0
    assert f["prepayment_ratio"] == 50.0
    assert f["payment_days"] == 120.0
    assert f["warranty_months"] == 6.0
    assert f["penalty_ratio"] == 40.0
    assert f["performance_start"] == "2025-02-10"
    assert f["auto_renewal"] is True
    assert f["dispute_mode"] is None  # 全文无争议解决条款


def test_six_tuple_format():
    doc = parse_document(RISKY_TEXT.encode("utf-8"), "test.txt")
    out = extract_facts_regex(doc)
    item = out["items"][0]
    assert set(item.keys()) == {"entity", "attribute", "value", "scope", "chunk", "confidence"}
    assert 0 <= item["confidence"] <= 1


# ---------------------------------------------------------------- 规则引擎

def test_rule_engine_triggers():
    doc = parse_document(RISKY_TEXT.encode("utf-8"), "test.txt")
    facts = extract_facts_regex(doc)["facts"]
    engine = RuleEngine.from_file()
    findings = engine.evaluate(doc, facts)
    titles = {f["title"] for f in findings}
    for expected in ("预付款比例过高", "付款周期过长", "质保期低于承诺/行业惯例",
                     "违约金比例过高（存在调减风险）", "合同金额大小写不一致",
                     "合同倒签（签订晚于履行起始日）", "缺少争议解决条款", "自动续约条款（存在遗忘终止风险）"):
        assert expected in titles, f"未触发规则: {expected}"


def test_score_and_hard_block():
    f1 = {"rule_id": "R001", "level": "高", "hard_block": False}
    f2 = {"rule_id": "R004", "level": "高", "hard_block": True}
    score, level, hard = score_findings([f1, f2], {"deduct_high": 15, "deduct_mid": 8, "deduct_low": 3,
                                                   "band_low": 80, "band_mid": 60, "band_high": 40})
    assert hard is True
    assert level == "高风险"
    assert score == 70


def test_llm_findings_half_weight():
    f = {"rule_id": "AGENT-法律合规", "level": "高", "hard_block": False}
    score, _, _ = score_findings([f], {"deduct_high": 15, "deduct_mid": 8, "deduct_low": 3,
                                       "band_low": 80, "band_mid": 60, "band_high": 40})
    assert score == 93  # 15//2=7


# ---------------------------------------------------------------- 端到端（无 LLM）

def test_end_to_end_risky_contract():
    settings = load_settings()
    analyzer = ContractAnalyzer(settings, llm=None)
    docx = (ROOT / "samples" / "高风险合同_示例.docx").read_bytes()
    result = analyzer.analyze(docx, "高风险合同_示例.docx", "采购合同",
                              commitment_warranty_months=36)
    assert result.score <= 20
    assert result.risk_level == "高风险"
    assert result.hard_block is True
    titles = {f["title"] for f in result.findings}
    assert "预付款比例过高" in titles
    assert "乙方命中供应商风险库（1项）" in titles
    assert "质保期低于投标承诺" in titles


def test_end_to_end_good_contract():
    settings = load_settings()
    analyzer = ContractAnalyzer(settings, llm=None)
    docx = (ROOT / "samples" / "合规合同_示例.docx").read_bytes()
    result = analyzer.analyze(docx, "合规合同_示例.docx", "服务合同")
    assert result.score == 100
    assert result.findings == []


def test_pdf_report_generation():
    """PDF 报告生成：有效 PDF 头、含批注、要素表不崩溃。"""
    from core.pdf_report import build_pdf_bytes
    settings = load_settings()
    analyzer = ContractAnalyzer(settings, llm=None)
    docx = (ROOT / "samples" / "高风险合同_示例.docx").read_bytes()
    result = analyzer.analyze(docx, "高风险合同_示例.docx", "采购合同",
                              commitment_warranty_months=36)
    meta = dict(result.meta)
    data = build_pdf_bytes(meta, result.facts, result.findings)
    assert data[:5] == b"%PDF-"          # 有效 PDF 头
    assert len(data) > 10000             # 有实质内容
    # law_refs 两种形态（dict 与 JSON 字符串）均不崩溃
    findings = result.findings + [{
        "rule_id": "T", "title": "测试", "category": "通用", "level": "中",
        "hard_block": False, "location": "全文", "evidence": "e", "suggestion": "s",
        "basis": "b", "law_refs": '[{"law": "内控指引16号", "point": "测试"}]',
        "confidence": 0.9, "status": "采纳"}]
    data2 = build_pdf_bytes(meta, result.facts, findings)
    assert data2[:5] == b"%PDF-"


def test_nav_redirect_via_apptest(tmp_db):
    """回归测试：审核流程写 nav_target，由 main() 在 radio 实例化前同步到 nav_page。
    此前直接写 st.session_state['nav_page'] 抛 StreamlitAPIException
    （带 key 的 widget 实例化后不可再改其值）。"""
    from streamlit.testing.v1 import AppTest
    db.register_user("导航测试", "pass123456")
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.session_state["logged_in"] = True
    at.session_state["username"] = "导航测试"
    at.session_state["nav_target"] = "📄 审核报告"
    at.run()
    assert not at.exception
    radio = at.sidebar.radio[0]
    assert radio.value == "📄 审核报告"


def test_pg_connection_failure_degrades(tmp_path, monkeypatch):
    """Cloud 场景：DATABASE_URL 连接失败 → 自动降级本地 SQLite，应用不崩溃。"""
    from core import db
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@127.0.0.1:1/nodb")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "_PG_DEGRADED", False)
    monkeypatch.setattr(db, "_PG_ERROR", "")
    db.init_db()  # 不抛异常
    assert db.is_degraded() is True
    assert db.get_degraded_note()  # 有脱敏后的失败原因
    assert "pass" not in db.get_degraded_note()  # 密码不泄露
    ok, _ = db.register_user("降级用户", "pass123456")
    assert ok and db.get_user("降级用户") is not None  # 降级后功能可用


def test_build_report_no_crash():
    """回归测试：报告生成必须兼容全部 law_refs 形态（含无条款号的制度引用）。
    此前真实合同触发内控指引类引用时曾抛 KeyError: 'article'。"""
    from core.report import build_report
    settings = load_settings()
    analyzer = ContractAnalyzer(settings, llm=None)
    docx = (ROOT / "samples" / "高风险合同_示例.docx").read_bytes()
    result = analyzer.analyze(docx, "高风险合同_示例.docx", "采购合同",
                              commitment_warranty_months=36)
    report_md = build_report(result)
    assert "【合同智能预审报告】" in report_md
    assert "内控指引16号" in report_md or "法条依据" in report_md
    # 全部 law_refs 形态均可格式化
    from core.report import _fmt_ref
    assert "第584条" in _fmt_ref({"law": "民法典", "article": 584, "point": "可预见性规则"})
    assert "内控指引16号" in _fmt_ref({"law": "内控指引16号", "point": "付款审批"})
    assert "民法典" in _fmt_ref({"law": "民法典"})
    assert _fmt_ref("《民法典》") == "《民法典》"


# ---------------------------------------------------------------- 数据库闭环

def test_user_and_quota_flow(tmp_db):
    ok, _ = db.register_user("测试用户", "pass123456")
    assert ok
    user, err = db.login_user("测试用户", "pass123456")
    assert err == "" and user["role"] == "admin"
    assert user["quota_total"] == 5 and user["review_count"] == 0

    review_id = db.insert_review(
        user["id"],
        {"file_name": "t.docx", "file_size": 100, "contract_type": "采购合同",
         "contract_no": "HT-1", "party_a": "甲", "party_b": "乙", "total_amount": "100",
         "score": 70, "risk_level": "中风险", "hard_block": False, "duration_ms": 100},
        "# 报告", "{}",
        [{"rule_id": "R001", "title": "预付款比例过高", "category": "商业条件风险",
          "level": "高", "hard_block": False, "location": "第三条", "evidence": "50%",
          "suggestion": "降低", "basis": "制度", "law_refs": [], "confidence": 0.9}])
    assert review_id > 0

    user2 = db.get_user("测试用户")
    assert user2["review_count"] == 1  # 审核即扣减额度

    review = db.get_review(review_id)
    assert review["findings"][0]["status"] == "待处理"
    db.set_finding_status(review["findings"][0]["id"], "采纳")
    review = db.get_review(review_id)
    assert review["findings"][0]["status"] == "采纳"  # 反馈闭环


def test_duplicate_username(tmp_db):
    db.register_user("重复名", "pass123456")
    ok, msg = db.register_user("重复名", "pass123456")
    assert not ok and "已存在" in msg


def test_consume_quota_no_record(tmp_db):
    """不留痕模式：消耗额度但不写入任何审核记录。"""
    db.register_user("不留痕用户", "pass123456")
    user, _ = db.login_user("不留痕用户", "pass123456")
    db.consume_quota(user["id"])
    u2 = db.get_user("不留痕用户")
    assert u2["review_count"] == 1
    assert db.list_reviews(u2["id"]) == []  # 无记录落库


def test_delete_review_with_quota_refund(tmp_db):
    """删除审核记录：记录与批注级联删除、额度返还、越权删除被拒绝。"""
    db.register_user("删除测试", "pass123456")
    user, _ = db.login_user("删除测试", "pass123456")
    review_id = db.insert_review(
        user["id"],
        {"file_name": "d.docx", "file_size": 10, "contract_type": "采购合同",
         "contract_no": "", "party_a": "", "party_b": "", "total_amount": "",
         "score": 80, "risk_level": "低风险", "hard_block": False, "duration_ms": 10},
        "# 报告", "{}",
        [{"rule_id": "R001", "title": "测试风险", "category": "通用", "level": "低",
          "hard_block": False, "location": "全文", "evidence": "e", "suggestion": "s",
          "basis": "b", "law_refs": [], "confidence": 0.9}])
    assert db.get_review(review_id) is not None

    # 越权删除：他人记录不可删
    db.register_user("越权者", "pass123456")
    other, _ = db.login_user("越权者", "pass123456")
    assert db.delete_review(review_id, other["id"]) is False
    assert db.get_review(review_id) is not None

    # 本人删除：级联删除 + 额度返还
    u_before = db.get_user("删除测试")
    assert u_before["review_count"] == 1
    assert db.delete_review(review_id, user["id"]) is True
    assert db.get_review(review_id) is None
    conn = db.get_db()
    n_findings = conn.execute("SELECT COUNT(*) c FROM findings WHERE review_id = ?", (review_id,)).fetchone()["c"]
    conn.close()
    assert n_findings == 0
    assert db.get_user("删除测试")["review_count"] == 0  # 已返还额度


def test_weak_password(tmp_db):
    ok, msg = db.register_user("弱密码用户", "123")
    assert not ok


META = {"file_name": "c.docx", "file_size": 10, "contract_type": "采购合同",
        "contract_no": "", "party_a": "", "party_b": "", "total_amount": "",
        "score": 80, "risk_level": "低风险", "hard_block": False, "duration_ms": 10}


def test_user_llm_settings(tmp_db, tmp_path, monkeypatch):
    """用户级 LLM 配置：默认 system，可切换为自有 Key；Key 加密入库、读取解密。"""
    from core import security
    monkeypatch.setattr(security, "SECRET_FILE", tmp_path / "secret.key")

    db.register_user("配置用户", "pass123456")
    user, _ = db.login_user("配置用户", "pass123456")
    assert user["llm_mode"] == "system"  # 默认系统 Key

    db.save_user_llm(user["id"], "custom", "sk-user-abc", "https://api.deepseek.com", "deepseek-chat")
    u2 = db.get_user("配置用户")
    assert u2["llm_mode"] == "custom"
    assert u2["llm_api_key"] != "sk-user-abc"            # 库内为密文
    assert u2["llm_api_key"].startswith(security.PREFIX)  # 带加密前缀
    assert security.decrypt_secret(u2["llm_api_key"]) == "sk-user-abc"  # 解密还原
    assert u2["llm_model"] == "deepseek-chat"

    db.save_user_llm(user["id"], "none", "sk-user-abc", "", "")
    assert db.get_user("配置用户")["llm_mode"] == "none"


def test_secret_encryption(tmp_path, monkeypatch):
    """加密模块：往返一致、幂等、明文兼容、空值安全。"""
    from core import security
    monkeypatch.setattr(security, "SECRET_FILE", tmp_path / "k.key")
    raw = "sk-1234567890abcdef"
    enc = security.encrypt_secret(raw)
    assert enc.startswith("enc:")
    assert security.decrypt_secret(enc) == raw
    assert security.encrypt_secret(enc) == enc          # 幂等：不重复加密
    assert security.decrypt_secret("") == ""             # 空值
    assert security.decrypt_secret("plain-old-key") == "plain-old-key"  # 历史明文兼容
    assert security.decrypt_secret(enc + "tampered") == ""  # 篡改密文安全失败


def test_free_review_no_quota_and_no_refund(tmp_db):
    """自有 Key / 纯规则引擎的免费审核：不扣额度；删除该记录也不返还额度。"""
    db.register_user("免费用户", "pass123456")
    user, _ = db.login_user("免费用户", "pass123456")

    rid = db.insert_review(user["id"], META, "# 报告", "{}", [], count_quota=False)
    assert db.get_user("免费用户")["review_count"] == 0  # 未扣额度
    conn = db.get_db()
    charged = conn.execute("SELECT charged FROM reviews WHERE id = ?", (rid,)).fetchone()["charged"]
    conn.close()
    assert charged == 0

    db.delete_review(rid, user["id"])
    assert db.get_user("免费用户")["review_count"] == 0  # 删除不返还（本就未扣）


def test_paid_unlock_flow(tmp_db):
    """付费解锁：记录解锁流水，审核记录不重复扣额度。"""
    db.register_user("付费用户", "pass123456")
    user, _ = db.login_user("付费用户", "pass123456")
    for _ in range(5):  # 用尽 5 份免费额度
        db.insert_review(user["id"], META, "# r", "{}", [])
    assert db.get_user("付费用户")["review_count"] == 5

    rid = db.insert_review(user["id"], META, "# 解锁报告", "{}", [], count_quota=False)
    unlock_id = db.insert_paid_unlock(user["id"], rid, 0.25, note="测试阶段模拟支付")
    assert unlock_id > 0
    assert db.get_user("付费用户")["review_count"] == 5  # 付费解锁不扣免费额度
    conn = db.get_db()
    row = conn.execute("SELECT amount, status FROM paid_unlocks WHERE id = ?", (unlock_id,)).fetchone()
    conn.close()
    assert row["amount"] == 0.25 and row["status"] == "paid"
