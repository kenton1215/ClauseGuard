# -*- coding: utf-8 -*-
"""全局配置：路径、LLM 设置、审核阈值。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
KNOWLEDGE_DIR = ROOT / "knowledge"
SETTINGS_FILE = DATA_DIR / "settings.json"
DB_PATH = DATA_DIR / "contract_review.db"

load_dotenv(ROOT / ".env")

DEFAULT_SETTINGS = {
    "llm": {
        # 优先级：settings.json > 环境变量（.env）> 默认值
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "temperature": 0.2,
        "timeout": 120,
        "max_chunks": 8,      # 单次审核最多调用 LLM 的文本片段数（成本控制）
    },
    "quota": {"free_reviews": 5},
    # 计费：使用系统默认 LLM 时，超过免费额度的每份审核解锁价格
    # enabled=False 为测试阶段（模拟支付）；接入真实支付渠道后置 True
    "billing": {"price_per_review": 0.25, "enabled": False},
    "thresholds": {
        "prepayment_ratio": 30.0,   # 预付款比例上限(%)
        "payment_days": 90.0,       # 付款期限上限(天)
        "penalty_ratio": 30.0,      # 违约金比例上限(%)
        "min_warranty_months": 12,  # 采购类最低质保期(月)
        "secrecy_years_max": 10,    # 保密期限上限(年)
        "max_contract_days": 1825,  # 履约期上限(5年)
    },
    "score": {
        "deduct_high": 15, "deduct_mid": 8, "deduct_low": 3,
        "band_low": 80, "band_mid": 60, "band_high": 40,
    },
}


EMPTY_SECRETS = {"llm": {}, "top": {}}


def _st_secrets() -> dict:
    """读取 Streamlit Cloud secrets（部署到 share.streamlit.io 时使用）。
    非 Streamlit 环境（CLI/测试）安全返回空结构。"""
    try:
        import streamlit as st
        s = getattr(st, "secrets", None)
        if not s:
            return dict(EMPTY_SECRETS)
        llm_cfg = dict(s.get("llm", {})) if isinstance(s.get("llm"), dict) else {}
        top = {k: s[k] for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
                                 "DASHSCOPE_API_KEY", "DATABASE_URL") if k in s}
        return {"llm": llm_cfg, "top": top}
    except Exception:  # noqa: BLE001
        return dict(EMPTY_SECRETS)


def _env_llm() -> dict:
    """从环境变量 / Streamlit secrets 读取 LLM 配置（兼容 .env 中的 DEEPSEEK_* 变量）。
    优先级：环境变量（本地 .env）> st.secrets（Streamlit Cloud）> 默认。"""
    out = {}
    secrets = _st_secrets()
    env_api = os.getenv("DEEPSEEK_API_KEY") or secrets["top"].get("DEEPSEEK_API_KEY")
    env_base = os.getenv("DEEPSEEK_BASE_URL") or secrets["top"].get("DEEPSEEK_BASE_URL")
    env_model = os.getenv("DEEPSEEK_MODEL") or secrets["top"].get("DEEPSEEK_MODEL")
    if env_api:
        out["api_key"] = env_api
        out["base_url"] = env_base or "https://api.deepseek.com"
        out["model"] = env_model or "deepseek-chat"
    elif os.getenv("DASHSCOPE_API_KEY") or secrets["top"].get("DASHSCOPE_API_KEY"):
        out["api_key"] = os.getenv("DASHSCOPE_API_KEY") or secrets["top"]["DASHSCOPE_API_KEY"]
        out["base_url"] = os.getenv("BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        out["model"] = "qwen-plus"
    elif secrets["llm"].get("api_key"):
        out.update(secrets["llm"])
    return out


def get_db_url() -> str:
    """外部数据库连接串（Streamlit Cloud 持久化用）。
    未配置时返回空串 → 使用本地 SQLite（数据随容器重启丢失，仅演示）。"""
    url = os.getenv("DATABASE_URL") or _st_secrets()["top"].get("DATABASE_URL") or ""
    return url.strip()


def load_settings() -> dict:
    """合并默认设置 + settings.json + 环境变量。"""
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))  # deep copy
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            for section in ("llm", "quota", "thresholds", "score"):
                if isinstance(saved.get(section), dict):
                    settings[section].update(saved[section])
        except Exception:
            pass
    settings["llm"].update(_env_llm())
    return settings


def save_settings(settings: dict) -> None:
    """保存到 settings.json（仅保留 UI 可编辑的 llm/quota/billing/thresholds 段）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps({k: settings.get(k, DEFAULT_SETTINGS[k]) for k in ("llm", "quota", "billing", "thresholds")},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def mask_key(api_key: str) -> str:
    """API Key 掩码显示：sk-f32d3e85...2aa0 -> sk-f3***2aa0（不暴露真实 Key）。"""
    key = (api_key or "").strip()
    if not key:
        return "（未配置）"
    if len(key) <= 8:
        return key[:2] + "***"
    return f"{key[:4]}***{key[-4:]}"
