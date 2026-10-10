# -*- coding: utf-8 -*-
"""敏感信息加密模块（用户自有 LLM API Key）。

方案：cryptography 库的 Fernet（AES-128-CBC + HMAC-SHA256 认证加密）。
- 加密密钥首次运行时自动生成于 data/secret.key（与数据库文件分离）；
- 密文带 "enc:" 前缀，便于识别与向后兼容（历史明文数据可继续读取）；
- 单机部署的标准折中：防止数据库文件泄露导致 Key 泄露；
  企业级升级路径：KMS/HSM 或硬件安全模块托管密钥。
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

from core.config import DATA_DIR

SECRET_FILE = DATA_DIR / "secret.key"
PREFIX = "enc:"


def _load_fernet():
    from cryptography.fernet import Fernet
    if not SECRET_FILE.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        # 仅当前用户可读写（Windows 上尽力而为）
        try:
            SECRET_FILE.write_bytes(key)
            os.chmod(SECRET_FILE, 0o600)
        except OSError:
            SECRET_FILE.write_bytes(key)
    return Fernet(SECRET_FILE.read_bytes())


def encrypt_secret(plaintext: str) -> str:
    """加密敏感字符串；空值原样返回。"""
    text = (plaintext or "").strip()
    if not text:
        return ""
    if text.startswith(PREFIX):
        return text  # 已加密
    return PREFIX + _load_fernet().encrypt(text.encode("utf-8")).decode("ascii")


def decrypt_secret(stored: str) -> str:
    """解密敏感字符串；无 enc: 前缀的（历史明文/空值）原样返回。"""
    text = (stored or "").strip()
    if not text:
        return ""
    if not text.startswith(PREFIX):
        return text
    try:
        return _load_fernet().decrypt(text[len(PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:  # noqa: BLE001  密钥变更等异常 → 返回空，避免程序崩溃
        return ""
