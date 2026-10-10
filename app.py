# -*- coding: utf-8 -*-
"""企业合同审核智能体 — Streamlit Web 应用。

功能：用户注册/登录（每账号免费审核5份）、合同上传解析、风险条款识别、
     修改建议、百分制评分、审核历史、人工反馈闭环（采纳/排除）、知识库浏览。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import db  # noqa: E402
from core.analyzer import ContractAnalyzer  # noqa: E402
from core.config import load_settings, save_settings, mask_key  # noqa: E402
from core.llm import LLMClient  # noqa: E402
from core.rag import LawIndex, ClauseLibrary, CaseLibrary  # noqa: E402
from core.report import build_report  # noqa: E402
from core.rule_engine import load_rules  # noqa: E402

st.set_page_config(page_title="企业合同智能审核", page_icon="📋", layout="wide")


# ================================================================ 初始化

@st.cache_resource
def get_db_ready() -> bool:
    db.init_db()
    return True


def current_user():
    uname = st.session_state.get("username")
    if uname:
        user = db.get_user(uname)
        if user and user["password_hash"]:
            return dict(user)
    return None


def remaining_quota(user: dict) -> int:
    return max(0, int(user["quota_total"]) - int(user["review_count"]))


def audit(action: str, detail: str = ""):
    u = current_user()
    if u:
        db.audit(u["id"], u["username"], action, detail)


@st.cache_data(show_spinner=False)
def rules_count() -> int:
    return len(load_rules().rules)


@st.cache_resource(show_spinner=False)
def get_law_index() -> LawIndex:
    return LawIndex()


# ================================================================ 登录/注册

def render_auth():
    st.markdown("<h1 style='text-align:center'>📋 企业合同审核智能体</h1>", unsafe_allow_html=True)
    st.markdown("<p style='text-align:center;color:#888'>六层工程体系 · 规则引擎 + LLM 多智能体 · 每账号免费审核 5 份</p>",
                unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1, 1.6, 1])
    with col2:
        tab_login, tab_register = st.tabs(["🔑 登录", "📝 注册"])
        with tab_login:
            with st.form("login_form"):
                username = st.text_input("用户名")
                password = st.text_input("密码", type="password")
                submitted = st.form_submit_button("登录", use_container_width=True)
                if submitted:
                    user, err = db.login_user(username, password)
                    if user is None:
                        st.error(err)
                    else:
                        st.session_state["logged_in"] = True
                        st.session_state["username"] = user["username"]
                        audit("登录", "用户登录")
                        st.rerun()
        with tab_register:
            with st.form("register_form"):
                new_username = st.text_input("新用户名（注册后不可修改）")
                new_password = st.text_input("新密码（至少6位）", type="password")
                confirm = st.text_input("确认密码", type="password")
                submitted = st.form_submit_button("注册并领取 5 次免费审核", use_container_width=True)
                if submitted:
                    if new_password != confirm:
                        st.error("两次输入的密码不一致。")
                    else:
                        ok, msg = db.register_user(new_username, new_password)
                        if ok:
                            st.success(msg)
                            audit("注册", f"新用户 {new_username}")
                        else:
                            st.error(msg)
        st.caption("💡 第一个注册的账号自动成为管理员，可在「管理设置」中配置 LLM 接口与用户额度。")


# ================================================================ 审核页

def resolve_llm(user: dict, settings: dict, enable_llm: bool) -> tuple:
    """按用户设置解析 LLM 客户端与计费模式。

    返回 (llm, billing_mode)：
      billing_mode='quota' 使用系统默认 LLM → 消耗免费额度，超出后 ¥0.25/份解锁
      billing_mode='free'  自有 API Key / 纯规则引擎 → 免费开放，不消耗额度
    """
    mode = user.get("llm_mode", "system")
    llm = None
    if enable_llm:
        from core.security import decrypt_secret
        custom_key = decrypt_secret(user.get("llm_api_key", ""))
        if mode == "custom" and custom_key:
            llm = LLMClient({
                "api_key": custom_key,
                "base_url": (user.get("llm_base_url") or "https://api.deepseek.com").strip(),
                "model": (user.get("llm_model") or "deepseek-chat").strip(),
                "temperature": settings["llm"].get("temperature", 0.2),
                "timeout": settings["llm"].get("timeout", 120),
            })
        elif mode == "system":
            llm = LLMClient(settings["llm"])
    billing_mode = "quota" if (llm is not None and llm.enabled and mode == "system") else "free"
    return llm, billing_mode


def render_upload():
    user = current_user()
    settings = load_settings()
    quota = int(user["quota_total"])
    used = int(user["review_count"])
    remaining = remaining_quota(user)
    mode = user.get("llm_mode", "system")
    price = float(settings.get("billing", {}).get("price_per_review", 0.25))

    st.markdown("## 📤 上传合同审核")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("风险规则", f"{rules_count()} 条")
    c2.metric("法条库", "3 部法律")
    if mode == "system":
        c3.metric("免费额度", f"{remaining} / {quota} 份")
        c4.metric("超出后", f"¥{price:.2f} / 份")
        if remaining <= 0:
            st.warning(f"免费额度已用尽：本次审核完成后需支付 ¥{price:.2f} 解锁查看结果。"
                       f"也可在「设置」中切换为自有 API Key 模式，完全免费开放。")
    else:
        c3.metric("审核模式", "免费开放")
        c4.metric("免费额度", "不消耗")

    with st.form("review_form"):
        uploaded = st.file_uploader("上传合同文件（支持 PDF / Word / TXT）",
                                    type=["pdf", "docx", "doc", "txt"])
        col1, col2, col3 = st.columns(3)
        contract_type = col1.selectbox("合同类型", ["采购合同", "销售合同", "服务合同", "技术开发合同",
                                                    "建设工程合同", "租赁合同", "其他"])
        commitment = col2.number_input("投标承诺质保期（月，选填）", min_value=0, max_value=120, value=0,
                                       help="如有投标文件承诺的质保期，填写后系统将自动比对合同质保期是否低于承诺。")
        enable_llm = col3.checkbox("本次启用 LLM 语义审查", value=(mode != "none"),
                                   help="依据「设置」中选定的模式（系统默认/自有Key）；未配置有效 Key 时自动降级为纯规则引擎。")
        no_trace = st.checkbox("🔒 不留痕模式（审核报告不保存，仅本次会话可见）", value=False,
                               help="勾选后本次审核不写入任何记录；关闭页面后报告无法回看。")
        submitted = st.form_submit_button("🚀 开始智能审核", use_container_width=True)

    if not submitted:
        st.info("操作指引：上传合同 → 选择类型 → 点击开始审核 → 查看风险批注并逐条处理（采纳/排除）。")
        return
    if uploaded is None:
        st.warning("请先上传合同文件。")
        return

    llm, billing_mode = resolve_llm(user, settings, enable_llm)

    progress_bar = st.progress(0.0, text="准备解析文档…")

    def progress(stage, frac):
        progress_bar.progress(frac, text=stage)

    with st.spinner("正在解析合同并执行多通道审核…"):
        analyzer = ContractAnalyzer(settings, llm)
        result = analyzer.analyze(
            uploaded.getvalue(), uploaded.name, contract_type,
            commitment_warranty_months=int(commitment) if commitment else None,
            progress=progress)
    progress_bar.progress(1.0, text="审核完成")

    if not result.report_md and not result.findings:
        st.error("解析失败：未能从文件中提取有效文本。")
        return

    report_md = build_report(result)
    import json as _json
    facts_json = _json.dumps(result.facts, ensure_ascii=False, default=str)
    st.session_state["nav_target"] = "📄 审核报告"  # 审核完成后自动切换到报告页

    # ---- 需要付费解锁：审核结果暂存会话，不落库
    if billing_mode == "quota" and remaining <= 0 and not no_trace:
        st.session_state["locked_review"] = {
            "report_md": report_md, "facts_json": facts_json,
            "findings": result.findings, "meta": result.meta,
            "file_name": result.meta["file_name"], "price": price,
            "score": result.score, "risk_level": result.risk_level,
        }
        st.session_state["review_done_msg"] = (
            f"🔒 审核已完成：综合评分 {result.score}/100（{result.risk_level}），"
            f"免费额度已用尽，支付 ¥{price:.2f} 可解锁查看完整报告。")
        st.session_state.pop("no_trace_review", None)
        audit("审核待解锁", f"{result.meta['file_name']} 评分{result.score}（免费额度已用尽）")
        st.rerun()

    # ---- 正常路径
    st.session_state.pop("locked_review", None)
    if billing_mode == "quota":
        msg_suffix = f"剩余免费额度 {max(0, remaining - 1)} 份。"
    elif llm is not None and llm.enabled:
        msg_suffix = "自有 Key 免费开放，不消耗额度。"
    else:
        msg_suffix = "纯规则引擎模式，免费开放，不消耗额度。"
    st.session_state["review_done_msg"] = (
        f"✅ 审核完成：综合评分 {result.score}/100（{result.risk_level}），{msg_suffix}")

    if no_trace:
        # 不留痕模式：不写入审核记录；使用系统默认 LLM 时仍消耗额度
        if billing_mode == "quota":
            db.consume_quota(user["id"])
        audit("不留痕审核", f"{result.meta['file_name']} 评分{result.score} 风险{len(result.findings)}项")
        st.session_state["no_trace_review"] = True
        st.session_state["last_report_md"] = report_md
        st.session_state["last_facts"] = result.facts
        st.session_state["last_meta"] = result.meta
        st.session_state["last_findings"] = result.findings
        st.session_state.pop("last_review_id", None)
    else:
        # 入库（系统默认 LLM 扣减额度；自有 Key / 纯规则免费）
        review_id = db.insert_review(
            user["id"], result.meta, report_md, facts_json, result.findings,
            count_quota=(billing_mode == "quota"))
        audit("合同审核", f"{result.meta['file_name']} 评分{result.score} 风险{len(result.findings)}项")
        st.session_state["last_review_id"] = review_id
        st.session_state["no_trace_review"] = False
    st.rerun()


# ================================================================ 报告展示

def pdf_bytes_for(meta: dict, facts: dict, findings: list[dict]) -> bytes:
    """审核数据 → PDF 报告 bytes。"""
    from core.pdf_report import build_pdf_bytes
    return build_pdf_bytes(meta, facts, findings)


def review_pdf_bytes(review: dict) -> bytes:
    """db review 记录 → PDF bytes。"""
    import json as _json
    try:
        facts = _json.loads(review.get("facts_json") or "{}")
    except ValueError:
        facts = {}
    meta = {
        "file_name": review.get("file_name"), "contract_type": review.get("contract_type"),
        "contract_no": review.get("contract_no"), "score": review.get("score"),
        "risk_level": review.get("risk_level"), "hard_block": review.get("hard_block"),
        "reviewed_at": review.get("created_at"),
    }
    return pdf_bytes_for(meta, facts, review.get("findings", []))



def render_finding_feedback(review_id: int, findings: list[dict]):
    st.markdown("### 🗳️ 风险批注处理（人工反馈闭环）")
    st.caption("对每条批注点击「采纳」或「排除」，处理结果将回流知识库，用于优化规则与案例库。")
    for i, f in enumerate(findings, 1):
        status = f.get("status", "待处理")
        icon = {"待处理": "⏳", "采纳": "✅", "排除": "❌"}.get(status, "⏳")
        cols = st.columns([7, 1, 1, 1])
        cols[0].markdown(f"**[风险#{i}]** {f.get('title', '')} · {f.get('level', '')}　{icon} {status}")
        if cols[1].button("采纳", key=f"adopt_{review_id}_{i}", disabled=status != "待处理"):
            db.set_finding_status(f["id"], "采纳")
            audit("反馈-采纳", f"{f.get('title')}")
            st.rerun()
        if cols[2].button("排除", key=f"reject_{review_id}_{i}", disabled=status != "待处理"):
            db.set_finding_status(f["id"], "排除")
            audit("反馈-排除", f"{f.get('title')}")
            st.rerun()
        cols[3].caption(f"{f.get('confidence', 0):.2f}")


def render_report_view():
    msg = st.session_state.pop("review_done_msg", None)
    if msg:
        st.success(msg)
    user = current_user()
    settings = load_settings()

    # 待付费解锁：免费额度已用尽（系统默认 LLM），显示摘要 + 解锁入口
    locked = st.session_state.get("locked_review")
    if locked:
        billing = settings.get("billing", {})
        price = float(locked.get("price", billing.get("price_per_review", 0.25)))
        billing_enabled = bool(billing.get("enabled", False))
        st.markdown("## 📄 审核报告（待解锁）")
        c1, c2 = st.columns(2)
        c1.metric("综合评分", f"{locked['score']}/100")
        c2.metric("风险等级", locked["risk_level"])
        st.warning(
            f"免费额度已用尽。本份完整报告需支付 **¥{price:.2f}** 解锁查看。\n\n"
            f"💡 免费替代方案：在「设置」中切换到**自有 API Key** 模式，即可免费开放使用，无需付费。")
        unlock_label = (f"🔓 支付 ¥{price:.2f} 解锁查看完整报告"
                        if billing_enabled else
                        f"🔓 模拟支付 ¥{price:.2f} 解锁（测试阶段，暂不产生真实扣费）")
        if st.button(unlock_label, use_container_width=True):
            import json as _json
            review_id = db.insert_review(
                user["id"], locked["meta"], locked["report_md"],
                locked["facts_json"], locked["findings"], count_quota=False)
            db.insert_paid_unlock(user["id"], review_id, price,
                                  note="测试阶段模拟支付" if not billing_enabled else "")
            st.session_state.pop("locked_review", None)
            st.session_state["last_review_id"] = review_id
            st.session_state["no_trace_review"] = False
            st.session_state["review_done_msg"] = f"✅ 已解锁，报告已保存至审核历史。（{'测试阶段模拟支付' if not billing_enabled else '已支付'} ¥{price:.2f}）"
            audit("付费解锁", f"review_id={review_id} 金额{price}（{'模拟' if not billing_enabled else '真实支付'}）")
            st.rerun()
        st.divider()
        st.caption("🔒 未解锁前仅展示评分摘要；解锁后报告保存至「审核历史」并可逐条处理批注。")
        return

    # 不留痕模式：报告仅存于本次会话内存，不落库
    if st.session_state.get("no_trace_review"):
        report_md = st.session_state.get("last_report_md", "")
        if not report_md:
            st.info("暂无审核结果，请先上传合同审核。")
            return
        st.markdown("## 📄 审核报告")
        st.info("🔒 不留痕模式：本次审核未保存任何记录（仅本次会话可见），关闭页面后无法回看；"
                "不留痕审核同样消耗额度。")
        st.markdown(report_md)
        st.divider()
        facts = st.session_state.get("last_facts", {})
        meta = dict(st.session_state.get("last_meta") or {})
        meta.setdefault("contract_no", facts.get("contract_no"))
        meta.setdefault("file_name", "不留痕审核")
        c1, c2 = st.columns([5, 1])
        c1.download_button(
            "⬇️ 下载审核报告（PDF）",
            pdf_bytes_for(meta, facts, st.session_state.get("last_findings", [])),
            file_name=f"审核报告_{facts.get('contract_no') or '不留痕'}.pdf",
            mime="application/pdf")
        c2.download_button("Markdown", report_md,
                           file_name=f"审核报告_{facts.get('contract_no') or '不留痕'}.md",
                           mime="text/markdown")
        return

    review_id = st.session_state.get("last_review_id")
    if not review_id:
        st.info("暂无审核结果，请先上传合同审核。")
        return
    review = db.get_review(review_id)  # 从数据库加载（findings 含自增 id，供反馈闭环使用）
    if not review:
        st.info("审核记录不存在，请重新审核。")
        return
    st.markdown("## 📄 审核报告")
    st.markdown(review["report_md"])
    st.divider()
    c1, c2, c3 = st.columns([4, 1, 1])
    c1.download_button("⬇️ 下载审核报告（PDF）", review_pdf_bytes(review),
                       file_name=f"审核报告_{review.get('contract_no') or review_id}.pdf",
                       mime="application/pdf")
    c2.download_button("Markdown", review["report_md"],
                       file_name=f"审核报告_{review.get('contract_no') or review_id}.md",
                       mime="text/markdown")
    if c3.button("🗑️ 删除该记录", help="删除后报告与批注不可恢复，并返还 1 次免费额度"):
        if db.delete_review(review_id, user["id"]):
            st.session_state.pop("last_review_id", None)
            st.session_state["flash"] = "🗑️ 审核记录已删除，已返还 1 次免费额度。"
            st.session_state["nav_target"] = "📤 合同审核"
            audit("删除审核记录", f"review_id={review_id}")
            st.rerun()
    st.divider()
    render_finding_feedback(review_id, review["findings"])


# ================================================================ 历史页

def render_history():
    user = current_user()
    st.markdown("## 🗂️ 审核历史")
    reviews = db.list_reviews(user["id"])
    if not reviews:
        st.info("暂无审核记录。")
        return
    options = [f"#{r['id']} · {r['file_name']} · {r['score']}分/{r['risk_level']} · {r['created_at']}"
               for r in reviews]
    picked = st.selectbox("选择审核记录查看", options, key="history_select")
    rid = int(picked.split(" · ")[0].lstrip("#"))
    review = db.get_review(rid)
    if not review:
        return
    st.markdown(review["report_md"])
    st.divider()
    c1, c2, c3 = st.columns([4, 1, 1])
    c1.download_button("⬇️ 下载报告（PDF）", review_pdf_bytes(review),
                       file_name=f"审核报告_{review.get('contract_no') or rid}.pdf",
                       mime="application/pdf")
    c2.download_button("Markdown", review["report_md"],
                       file_name=f"审核报告_{review.get('contract_no') or rid}.md",
                       mime="text/markdown")
    if c3.button("🗑️ 删除该记录", help="删除后不可恢复，并返还 1 次免费额度"):
        if db.delete_review(rid, user["id"]):
            st.session_state["flash"] = "🗑️ 审核记录已删除，已返还 1 次免费额度。"
            if st.session_state.get("last_review_id") == rid:
                st.session_state.pop("last_review_id", None)
            audit("删除审核记录", f"review_id={rid}")
            st.rerun()
    st.divider()
    render_finding_feedback(rid, review["findings"])


# ================================================================ 知识库页

def render_knowledge():
    st.markdown("## 📚 知识库")
    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
        ["法律法规库", "风险规则库", "标准条款库", "历史案例库", "供应商库", "法条检索"])

    with tab1:
        law_index = get_law_index()
        from collections import Counter
        counts = Counter(a["law"] for a in law_index.articles)
        for law, n in counts.items():
            st.markdown(f"**《{law}》**：{n} 条逐条原文（结构化 JSON）")
        st.caption("来源：最高人民检察院网站/商务部全球法规网转载的国家法律法规数据库全文。"
                   "支持版本更新：替换 knowledge/legal/ 下对应 JSON 即可。")

    with tab2:
        rules = load_rules().rules
        st.caption(f"共 {len(rules)} 条可配置规则，修改 knowledge/rules/risk_rules.json 后即时生效。")
        st.dataframe([
            {"ID": r.get("id"), "规则": r.get("title"), "类别": r.get("category"),
             "等级": r.get("risk_level"), "刚性": "是" if r.get("hard_block") else "否"}
            for r in rules], use_container_width=True, hide_index=True)

    with tab3:
        lib = ClauseLibrary().items
        st.caption(f"共 {len(lib)} 类标准条款，每类含主选条款与备选条款。")
        for item in lib:
            with st.expander(f"📌 {item.get('type', '')}（{item.get('category', '通用')}）"):
                st.markdown(f"**主选条款**：{item.get('preferred_text', '')}")
                st.markdown(f"**备选条款**：{item.get('alternate_text', '')}")

    with tab4:
        cases = CaseLibrary().items
        st.caption(f"共 {len(cases)} 条历史案例（条款-风险标注对）。")
        st.dataframe([
            {"编号": c.get("case_id"), "类型": c.get("contract_type"), "问题": c.get("issue"),
             "等级": c.get("risk_level"), "处理": c.get("final_decision")}
            for c in cases], use_container_width=True, hide_index=True)

    with tab5:
        sup_path = ROOT / "knowledge" / "supplier" / "supplier_db.json"
        if sup_path.exists():
            data = json.loads(sup_path.read_text(encoding="utf-8"))
            st.caption(f"共 {len(data)} 条供应商记录。P1 可对接国家企业信用信息公示系统/天眼查 API 自动核验。")
            st.dataframe([
                {"名称": s.get("name"), "信用代码": s.get("credit_code"),
                 "风险等级": s.get("risk", {}).get("level"), "风险标记": "；".join(s.get("risk", {}).get("flags", []))}
                for s in data], use_container_width=True, hide_index=True)
        else:
            st.info("供应商库未初始化。请按 knowledge/supplier/README.md 的格式创建 supplier_db.json。")

    with tab6:
        st.caption("法条语义检索（基于逐条原文索引，零外部依赖）")
        query = st.text_input("输入审查概念（如：违约金 / 不可抗力 / 协议管辖）", value="违约金")
        if query:
            law_index = get_law_index()
            for a in law_index.retrieve(query, top_k=5):
                st.markdown(f"**《{a['law']}》{a['article']}**（{a['chapter']}）")
                st.markdown(f">{a['text'][:200]}")
                st.divider()


# ================================================================ 设置页（所有用户可访问）

def render_settings():
    user = current_user()
    settings = load_settings()
    st.markdown("## ⚙️ 设置")
    tab1, tab2, tab3, tab4 = st.tabs(
        ["🔑 LLM 接口配置", "💳 额度与解锁", "👥 用户额度管理", "📜 审计日志"])

    # ---------------- tab1: LLM 配置（所有用户）
    with tab1:
        st.caption("每个账号可独立选择审核模式：系统默认 LLM / 自有 API Key / 关闭 LLM（纯规则引擎）。"
                   "切换后立即生效。")
        cur_mode = user.get("llm_mode", "system")
        mode = st.radio(
            "审核模式",
            ["system", "custom", "none"],
            index=["system", "custom", "none"].index(cur_mode) if cur_mode in ("system", "custom", "none") else 0,
            format_func=lambda m: {"system": "🟢 系统默认 LLM（前5份免费，超出 ¥0.25/份解锁）",
                                   "custom": "🔵 使用我的 API Key（完全免费开放）",
                                   "none": "⚪ 关闭 LLM（纯规则引擎，免费开放）"}[m],
            key="llm_mode_radio")

        if mode == "system":
            sys_key = settings["llm"].get("api_key", "")
            if sys_key:
                st.success(f"✅ 系统已配置默认 LLM：{mask_key(sys_key)}　模型：{settings['llm'].get('model', '')}　"
                           f"端点：{settings['llm'].get('base_url', '')}")
                st.caption("系统默认 Key 由平台提供（掩码显示，不可见明文）。使用该模式：前 5 份免费，之后每份 ¥0.25 解锁。")
            else:
                st.warning("系统默认 LLM 尚未配置，请联系管理员。您可切换到「使用我的 API Key」模式。")

        elif mode == "custom":
            from core.security import decrypt_secret
            saved_key = decrypt_secret(user.get("llm_api_key", ""))
            st.caption("🔐 安全说明：您的 API Key 将以**加密形式**（AES-Fernet 认证加密）保存在本地数据库中，"
                       "加密密钥与数据库分离存储，页面仅显示掩码，其他用户无法查看，"
                       "仅用于您本人账号的合同审核调用。")
            if saved_key:
                st.success(f"✅ 已保存自有 Key：{mask_key(saved_key)}（加密存储）。"
                           f"如需更换请直接输入新 Key 保存。")
            with st.form("user_llm_form"):
                api_key = st.text_input("API Key（输入新 Key 可覆盖已保存的 Key，留空保持不变）",
                                        value="", type="password", key="user_api_key_input",
                                        help="您自己申请的 DeepSeek/通义千问/OpenAI 兼容 Key，"
                                             "加密存储于本地数据库，仅您本人账号可用。")
                base_url = st.text_input("Base URL", value=user.get("llm_base_url") or "https://api.deepseek.com")
                model = st.text_input("模型", value=user.get("llm_model") or "deepseek-chat")
                saved = st.form_submit_button("💾 保存我的 Key", use_container_width=True)
            if saved:
                new_key = api_key.strip() or saved_key
                if not new_key:
                    st.error("尚未保存任何 API Key，请先输入。")
                else:
                    db.save_user_llm(user["id"], mode, new_key, base_url, model)
                    audit("设置", "保存自有 LLM Key")
                    st.success("已加密保存，审核将使用您的 API Key，免费开放。")
                    st.rerun()
            if st.button("🔌 测试连接（使用上方填写的 Key，未填写则测试已保存的 Key）"):
                from core.security import decrypt_secret
                test_key = st.session_state.get("user_api_key_input") or decrypt_secret(user.get("llm_api_key", ""))
                llm = LLMClient({**settings["llm"], "api_key": test_key,
                                 "base_url": base_url or "https://api.deepseek.com",
                                 "model": model or "deepseek-chat"})
                if not llm.enabled:
                    st.warning("请先填写 API Key。")
                else:
                    with st.spinner("正在测试…"):
                        reply = llm.chat_text("你是测试助手。", "请回复：连接成功")
                    st.success("✅ " + reply) if reply else st.error("连接失败，请检查 Key/端点/模型。")

        elif mode == "none":
            st.info("已关闭 LLM：审核将使用纯规则引擎（28 条硬规则+评分+报告），免费开放、不消耗额度。")

        if mode != cur_mode and mode != "custom":
            if st.button(f"✅ 应用「{['系统默认 LLM', '使用我的 API Key', '关闭 LLM'][['system', 'custom', 'none'].index(mode)]}」模式"):
                db.save_user_llm(user["id"], mode, user.get("llm_api_key", ""),
                                 user.get("llm_base_url", ""), user.get("llm_model", ""))
                audit("设置", f"切换审核模式为 {mode}")
                st.success("已切换，立即生效。")
                st.rerun()

    # ---------------- tab2: 额度与解锁（所有用户）
    with tab2:
        price = float(settings.get("billing", {}).get("price_per_review", 0.25))
        billing_enabled = bool(settings.get("billing", {}).get("enabled", False))
        st.markdown("### 计费规则")
        st.markdown(
            f"- 🟢 系统默认 LLM：每账号前 **{int(user['quota_total'])} 份免费**，超出后 **¥{price:.2f}/份** 解锁查看结果"
            f"{'（支付渠道接入中）' if not billing_enabled else ''}；\n"
            f"- 🔵 自有 API Key：**完全免费开放**，不消耗额度（费用由您的 Key 承担）；\n"
            f"- ⚪ 纯规则引擎：**免费开放**，不消耗额度。")
        c1, c2 = st.columns(2)
        c1.metric("我的免费额度剩余", f"{remaining_quota(user)} / {int(user['quota_total'])} 份")
        c2.metric("累计审核", f"{int(user['review_count'])} 份")
        conn = db.get_db()
        unlocks = conn.execute(
            "SELECT amount, status, note, created_at FROM paid_unlocks WHERE user_id = ? ORDER BY id DESC LIMIT 20",
            (user["id"],)).fetchall()
        conn.close()
        if unlocks:
            st.markdown("### 我的解锁记录")
            st.dataframe([dict(r) for r in unlocks], use_container_width=True, hide_index=True)

    # ---------------- tab3: 用户额度管理（仅管理员）
    with tab3:
        if user["role"] != "admin":
            st.info("仅管理员可查看本页。")
        else:
            st.caption("每账号默认 5 份免费审核额度，可在此调整或重置。")
            for u in db.list_users():
                cols = st.columns([3, 1, 1, 1, 1, 2])
                cols[0].write(f"**{u['username']}** {'👑管理员' if u['role'] == 'admin' else ''}")
                cols[1].metric("额度", u["quota_total"])
                cols[2].metric("已用", u["review_count"])
                new_quota = cols[3].number_input("额度", 0, 999, u["quota_total"], key=f"q_{u['id']}", label_visibility="collapsed")
                if cols[4].button("重置", key=f"reset_{u['id']}"):
                    db.set_quota(u["username"], int(new_quota), reset_count=True)
                    audit("设置", f"重置用户{u['username']}额度为{int(new_quota)}")
                    st.rerun()

    # ---------------- tab4: 审计日志（仅管理员）
    with tab4:
        if user["role"] != "admin":
            st.info("仅管理员可查看本页。")
        else:
            conn = db.get_db()
            rows = conn.execute(
                "SELECT username, action, detail, created_at FROM audit_log ORDER BY id DESC LIMIT 50").fetchall()
            conn.close()
            st.dataframe([dict(r) for r in rows], use_container_width=True, hide_index=True)


# ================================================================ 主流程

def main():
    get_db_ready()
    if db.is_degraded():
        st.warning(
            "⚠️ 外部数据库连接失败，已降级为临时本地存储（容器重启后数据会丢失）。\n\n"
            f"失败原因：{db.get_degraded_note()}\n\n"
            "请检查 Secrets 中的 DATABASE_URL：\n"
            "- 确认连接串格式（postgresql://user:password@host:5432/dbname）；\n"
            "- **Supabase 请使用 Session Pooler 连接串**（xxx.pooler.supabase.com，IPv4），"
            "Direct connection 为 IPv6 地址，Streamlit Cloud 无法访问；\n"
            "- Neon 请使用 Pooled connection 连接串；\n"
            "- 连接串需包含 `?sslmode=require`。\n\n"
            "修复后重启应用即可恢复持久化存储。详见 docs/部署StreamlitCloud.md。")
    if not st.session_state.get("logged_in"):
        render_auth()
        return

    user = current_user()
    if user is None:
        st.session_state.clear()
        st.rerun()

    st.sidebar.markdown("### 👤 账户中心")
    st.sidebar.write(f"**{user['username']}** {'（管理员）' if user['role'] == 'admin' else ''}")
    quota = int(user["quota_total"])
    used = int(user["review_count"])
    mode = user.get("llm_mode", "system")
    mode_badge = {"system": "🟢 系统默认 LLM", "custom": "🔵 自有 API Key", "none": "⚪ 纯规则引擎"}.get(mode, mode)
    st.sidebar.caption(f"审核模式：{mode_badge}")
    if mode == "system":
        st.sidebar.progress(min(1.0, used / quota) if quota else 0.0)
        st.sidebar.caption(f"免费额度：{max(0, quota - used)} / {quota} 份（超出 ¥0.25/份）")
    else:
        st.sidebar.caption("免费开放使用，不消耗额度")
    st.sidebar.divider()

    # 跳转同步：流程中写入 nav_target，在此（radio 实例化之前）同步到 nav_page。
    # Streamlit 不允许在带 key 的 widget 实例化之后再修改其 session_state 值。
    target = st.session_state.pop("nav_target", None)
    if target:
        st.session_state["nav_page"] = target
    st.session_state.setdefault("nav_page", "📤 合同审核")
    page = st.sidebar.radio("导航", ["📤 合同审核", "📄 审核报告", "🗂️ 审核历史", "📚 知识库", "⚙️ 设置"],
                            key="nav_page")
    if st.sidebar.button("退出登录", use_container_width=True):
        audit("退出", "退出登录")
        st.session_state.clear()
        st.rerun()

    flash = st.session_state.pop("flash", None)  # 跨页提示（如删除记录成功）
    if flash:
        st.success(flash)

    if page == "📤 合同审核":
        render_upload()
    elif page == "📄 审核报告":
        render_report_view()
    elif page == "🗂️ 审核历史":
        render_history()
    elif page == "📚 知识库":
        render_knowledge()
    elif page == "⚙️ 设置":
        render_settings()


if __name__ == "__main__":
    main()
