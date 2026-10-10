# -*- coding: utf-8 -*-
"""数据层：用户、审核记录、风险批注、审计日志、付费解锁。

双数据库适配（接口一致，业务代码无感知）：
- 本地模式（默认）：SQLite 文件 data/contract_review.db —— 适合单机/内网部署；
- 云端模式：配置 DATABASE_URL（PostgreSQL，如 Supabase/Neon 免费库）——
  Streamlit Cloud 文件系统为临时盘，SQLite 会随容器重启丢失，务必用外部库持久化。

安全说明：
- 密码使用 PBKDF2-HMAC-SHA256 加盐哈希（旧版 sha256 哈希自动兼容）；
- 用户自有 LLM API Key 加密存储（core.security）。
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from datetime import datetime

from core.config import DB_PATH, get_db_url

_lock = threading.Lock()


def _is_pg() -> bool:
    return get_db_url().startswith(("postgresql://", "postgres://"))


def _sql(sql: str) -> str:
    """占位符适配：SQLite 用 ?，PostgreSQL 用 %s。"""
    return sql.replace("?", "%s") if _is_pg() else sql


# ---------------------------------------------------------------- 表结构

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL DEFAULT '',
    hash_algo TEXT NOT NULL DEFAULT 'pbkdf2',
    role TEXT NOT NULL DEFAULT 'user',
    quota_total INTEGER NOT NULL DEFAULT 5,
    review_count INTEGER NOT NULL DEFAULT 0,
    llm_mode TEXT NOT NULL DEFAULT 'system',
    llm_api_key TEXT NOT NULL DEFAULT '',
    llm_base_url TEXT NOT NULL DEFAULT '',
    llm_model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    file_name TEXT NOT NULL,
    file_size INTEGER DEFAULT 0,
    contract_type TEXT DEFAULT '',
    contract_name TEXT DEFAULT '',
    contract_no TEXT DEFAULT '',
    party_a TEXT DEFAULT '',
    party_b TEXT DEFAULT '',
    total_amount TEXT DEFAULT '',
    score INTEGER DEFAULT 100,
    risk_level TEXT DEFAULT '',
    hard_block INTEGER DEFAULT 0,
    issue_count INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    report_md TEXT DEFAULT '',
    facts_json TEXT DEFAULT '{}',
    findings_json TEXT DEFAULT '[]',
    charged INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    rule_id TEXT DEFAULT '',
    category TEXT DEFAULT '',
    title TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT '中',
    hard_block INTEGER DEFAULT 0,
    location TEXT DEFAULT '',
    evidence TEXT DEFAULT '',
    suggestion TEXT DEFAULT '',
    basis TEXT DEFAULT '',
    law_refs TEXT DEFAULT '[]',
    confidence REAL DEFAULT 0,
    status TEXT NOT NULL DEFAULT '待处理',
    feedback_at TEXT DEFAULT '',
    FOREIGN KEY(review_id) REFERENCES reviews(id)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER DEFAULT 0,
    username TEXT DEFAULT '',
    action TEXT NOT NULL,
    detail TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS paid_unlocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    review_id INTEGER,
    amount REAL NOT NULL DEFAULT 0.25,
    status TEXT NOT NULL DEFAULT 'paid',
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_reviews_user ON reviews(user_id);
CREATE INDEX IF NOT EXISTS idx_findings_review ON findings(review_id);
"""

# PostgreSQL 方言（SERIAL 主键 + CURRENT_TIMESTAMP）
SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL DEFAULT '',
    hash_algo TEXT NOT NULL DEFAULT 'pbkdf2',
    role TEXT NOT NULL DEFAULT 'user',
    quota_total INTEGER NOT NULL DEFAULT 5,
    review_count INTEGER NOT NULL DEFAULT 0,
    llm_mode TEXT NOT NULL DEFAULT 'system',
    llm_api_key TEXT NOT NULL DEFAULT '',
    llm_base_url TEXT NOT NULL DEFAULT '',
    llm_model TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS reviews (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    file_name TEXT NOT NULL,
    file_size INTEGER DEFAULT 0,
    contract_type TEXT DEFAULT '',
    contract_name TEXT DEFAULT '',
    contract_no TEXT DEFAULT '',
    party_a TEXT DEFAULT '',
    party_b TEXT DEFAULT '',
    total_amount TEXT DEFAULT '',
    score INTEGER DEFAULT 100,
    risk_level TEXT DEFAULT '',
    hard_block INTEGER DEFAULT 0,
    issue_count INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    report_md TEXT DEFAULT '',
    facts_json TEXT DEFAULT '{}',
    findings_json TEXT DEFAULT '[]',
    charged INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS findings (
    id SERIAL PRIMARY KEY,
    review_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    rule_id TEXT DEFAULT '',
    category TEXT DEFAULT '',
    title TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT '中',
    hard_block INTEGER DEFAULT 0,
    location TEXT DEFAULT '',
    evidence TEXT DEFAULT '',
    suggestion TEXT DEFAULT '',
    basis TEXT DEFAULT '',
    law_refs TEXT DEFAULT '[]',
    confidence REAL DEFAULT 0,
    status TEXT NOT NULL DEFAULT '待处理',
    feedback_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    user_id INTEGER DEFAULT 0,
    username TEXT DEFAULT '',
    action TEXT NOT NULL,
    detail TEXT DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS paid_unlocks (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    review_id INTEGER,
    amount REAL NOT NULL DEFAULT 0.25,
    status TEXT NOT NULL DEFAULT 'paid',
    note TEXT DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_reviews_user ON reviews(user_id);
CREATE INDEX IF NOT EXISTS idx_findings_review ON findings(review_id);
"""

# 兼容旧版 Copilot 生成的表结构（仅 SQLite 迁移用）
MIGRATIONS = [
    """INSERT OR IGNORE INTO users (username, password_hash, salt, hash_algo, role, quota_total, review_count)
       SELECT username, password_hash, '', 'sha256', 'user', 5, review_count
       FROM legacy_users WHERE 1""",
    """INSERT OR IGNORE INTO reviews
       (user_id, file_name, score, risk_level, issue_count, created_at)
       SELECT u.id, r.file_name, r.score, r.risk_level, r.issue_count, r.reviewed_at
       FROM legacy_review_history r JOIN users u ON u.username = r.username""",
]


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    try:
        has_legacy = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_users'").fetchone()
        if has_legacy:
            for sql in MIGRATIONS:
                try:
                    conn.execute(sql)
                except sqlite3.Error:
                    pass
            conn.commit()
    except sqlite3.Error:
        pass


def _migrate_llm_columns(conn: sqlite3.Connection) -> None:
    """旧版 SQLite 表补充列（幂等）。"""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        for col, ddl in (("llm_mode", "TEXT NOT NULL DEFAULT 'system'"),
                         ("llm_api_key", "TEXT NOT NULL DEFAULT ''"),
                         ("llm_base_url", "TEXT NOT NULL DEFAULT ''"),
                         ("llm_model", "TEXT NOT NULL DEFAULT ''")):
            if col not in cols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        rcols = {r[1] for r in conn.execute("PRAGMA table_info(reviews)").fetchall()}
        if "charged" not in rcols:
            conn.execute("ALTER TABLE reviews ADD COLUMN charged INTEGER NOT NULL DEFAULT 1")
        conn.commit()
    except sqlite3.Error:
        pass


def init_db() -> None:
    if _is_pg():
        conn = get_db()
        cur = conn.cursor()
        for stmt in SCHEMA_PG.split(";"):
            if stmt.strip():
                cur.execute(stmt)
        conn.commit()
        _close(conn)
        return

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_SQLITE)
    conn.commit()
    # 旧表改名迁移（幂等）
    try:
        conn.execute("ALTER TABLE users RENAME TO legacy_users")
        conn.executescript(SCHEMA_SQLITE)
    except sqlite3.Error:
        pass  # 新表已存在
    try:
        conn.execute("ALTER TABLE review_history RENAME TO legacy_review_history")
    except sqlite3.Error:
        pass
    _migrate_legacy(conn)
    _migrate_llm_columns(conn)
    conn.commit()
    _close(conn)


def get_db():
    """统一连接入口：SQLite（本地）或 PostgreSQL（云端，DATABASE_URL）。"""
    if _is_pg():
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(get_db_url(), row_factory=dict_row, autocommit=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _close(conn) -> None:
    """统一关闭：SQLite 提交事务（保证写入持久化与原子性）；psycopg autocommit 下为 no-op。"""
    try:
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
    conn.close()


def _rows(cur, n: int | None = None):
    """兼容取行：psycopg 返回 list[dict]，sqlite 返回 list[Row]。"""
    rows = cur.fetchall() if n is None else cur.fetchmany(n)
    return [dict(r) if not isinstance(r, dict) else r for r in rows]


# ---------------------------------------------------------------- 用户

def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt.encode("utf-8"), 120_000).hex()
    return digest, salt


def verify_password(user, password: str) -> bool:
    algo = user["hash_algo"] if "hash_algo" in user.keys() else "sha256"
    if algo == "pbkdf2":
        digest, _ = hash_password(password, user["salt"])
        return secrets.compare_digest(digest, user["password_hash"])
    # 兼容旧版 sha256 无盐哈希
    legacy = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return secrets.compare_digest(legacy, user["password_hash"])


def register_user(username: str, password: str) -> tuple[bool, str]:
    username = username.strip()
    if not username or len(username) > 50:
        return False, "用户名不能为空且不超过50个字符。"
    if len(password) < 6:
        return False, "密码长度至少为6位。"
    digest, salt = hash_password(password)
    conn = get_db()
    try:
        is_first = conn.execute(_sql("SELECT COUNT(*) c FROM users")).fetchone()["c"] == 0
        role = "admin" if is_first else "user"  # 首个注册用户为管理员
        conn.execute(
            _sql("INSERT INTO users (username, password_hash, salt, hash_algo, role) VALUES (?,?,?,?,?)"),
            (username, digest, salt, "pbkdf2", role))
        return True, ("注册成功，您已成为管理员，请登录。" if is_first else "注册成功，请登录。")
    except Exception as e:  # noqa: BLE001  （sqlite3.IntegrityError / psycopg.errors.UniqueViolation）
        if "UNIQUE" in str(e).upper() or isinstance(e, sqlite3.IntegrityError):
            return False, "用户名已存在，请更换。"
        return False, f"注册失败：{e}"
    finally:
        _close(conn)


def login_user(username: str, password: str) -> tuple[dict | None, str]:
    conn = get_db()
    user = conn.execute(_sql("SELECT * FROM users WHERE username = ?"), (username.strip(),)).fetchone()
    _close(conn)
    if not user:
        return None, "用户不存在。"
    user = dict(user) if not isinstance(user, dict) else user
    if not verify_password(user, password):
        return None, "密码错误。"
    return user, ""


def get_user(username: str) -> dict | None:
    conn = get_db()
    user = conn.execute(_sql("SELECT * FROM users WHERE username = ?"), (username,)).fetchone()
    _close(conn)
    if not user:
        return None
    return dict(user) if not isinstance(user, dict) else user


def list_users() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        _sql("SELECT id, username, role, quota_total, review_count, created_at FROM users ORDER BY id")).fetchall()
    _close(conn)
    return [dict(r) if not isinstance(r, dict) else r for r in rows]


def save_user_llm(user_id: int, mode: str, api_key: str, base_url: str, model: str) -> None:
    """保存用户级 LLM 配置（mode: system=系统默认 / custom=自有Key / none=关闭LLM）。
    API Key 加密后入库（core.security.encrypt_secret），数据库泄露不直接暴露 Key。"""
    from core.security import encrypt_secret
    conn = get_db()
    conn.execute(
        _sql("UPDATE users SET llm_mode=?, llm_api_key=?, llm_base_url=?, llm_model=? WHERE id=?"),
        (mode, encrypt_secret(api_key), base_url.strip(), model.strip(), user_id))
    _close(conn)


def insert_paid_unlock(user_id: int, review_id: int, amount: float, note: str = "") -> int:
    """记录付费解锁（支付渠道后续接入，当前 status='paid' 为测试阶段模拟支付）。"""
    conn = get_db()
    cur = conn.execute(
        _sql("INSERT INTO paid_unlocks (user_id, review_id, amount, status, note) VALUES (?,?,?,?,?) RETURNING id"),
        (user_id, review_id, amount, "paid", note))
    uid = cur.fetchone()["id"]
    _close(conn)
    return uid


def set_quota(username: str, quota: int, reset_count: bool) -> None:
    conn = get_db()
    if reset_count:
        conn.execute(_sql("UPDATE users SET quota_total = ?, review_count = 0 WHERE username = ?"), (quota, username))
    else:
        conn.execute(_sql("UPDATE users SET quota_total = ? WHERE username = ?"), (quota, username))
    _close(conn)


# ---------------------------------------------------------------- 审核记录

def insert_review(user_id: int, meta: dict, report_md: str,
                  facts_json: str, findings: list[dict], count_quota: bool = True) -> int:
    """写入审核记录。count_quota=False 表示本次审核不消耗免费额度
    （自有 Key 免费开放 / 付费解锁 / 纯规则引擎免费），并标记 charged=0
    以便删除记录时不做额度返还。"""
    conn = get_db()
    cur = conn.execute(
        _sql("""INSERT INTO reviews (user_id, file_name, file_size, contract_type, contract_name,
            contract_no, party_a, party_b, total_amount, score, risk_level, hard_block,
            issue_count, duration_ms, report_md, facts_json, findings_json, charged)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id"""),
        (user_id, meta["file_name"], meta.get("file_size", 0), meta.get("contract_type", ""),
         meta.get("contract_name", ""), meta.get("contract_no", ""), meta.get("party_a", ""),
         meta.get("party_b", ""), meta.get("total_amount", ""), meta.get("score", 100),
         meta.get("risk_level", ""), 1 if meta.get("hard_block") else 0,
         len(findings), meta.get("duration_ms", 0), report_md, facts_json,
         json_dumps(findings), 1 if count_quota else 0))
    review_id = cur.fetchone()["id"]
    for i, f in enumerate(findings, start=1):
        conn.execute(
            _sql("""INSERT INTO findings (review_id, seq, rule_id, category, title, level, hard_block,
                location, evidence, suggestion, basis, law_refs, confidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"""),
            (review_id, i, f.get("rule_id", ""), f.get("category", ""), f.get("title", ""),
             f.get("level", "中"), 1 if f.get("hard_block") else 0, f.get("location", ""),
             f.get("evidence", ""), f.get("suggestion", ""), f.get("basis", ""),
             json_dumps(f.get("law_refs", [])), f.get("confidence", 0)))
    if count_quota:
        conn.execute(_sql("UPDATE users SET review_count = review_count + 1 WHERE id = ?"), (user_id,))
    _close(conn)
    return review_id


def list_reviews(user_id: int) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        _sql("""SELECT id, file_name, contract_type, contract_no, party_a, party_b, score,
            risk_level, hard_block, issue_count, duration_ms, created_at
           FROM reviews WHERE user_id = ? ORDER BY id DESC"""), (user_id,)).fetchall()
    _close(conn)
    return [dict(r) if not isinstance(r, dict) else r for r in rows]


def get_review(review_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute(_sql("SELECT * FROM reviews WHERE id = ?"), (review_id,)).fetchone()
    findings = conn.execute(
        _sql("SELECT * FROM findings WHERE review_id = ? ORDER BY seq"), (review_id,)).fetchall()
    _close(conn)
    if not row:
        return None
    data = dict(row) if not isinstance(row, dict) else row
    data["findings"] = [dict(f) if not isinstance(f, dict) else f for f in findings]
    return data


def set_finding_status(finding_id: int, status: str, note: str = "") -> None:
    conn = get_db()
    conn.execute(
        _sql("UPDATE findings SET status = ?, feedback_at = ? WHERE id = ?"),
        (status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), finding_id))
    _close(conn)


def consume_quota(user_id: int) -> None:
    """不留痕审核：只消耗额度，不写入审核记录。"""
    conn = get_db()
    conn.execute(_sql("UPDATE users SET review_count = review_count + 1 WHERE id = ?"), (user_id,))
    _close(conn)


def delete_review(review_id: int, user_id: int) -> bool:
    """删除某条审核记录及其风险批注（仅限本人记录）。
    仅当该记录实际消耗了免费额度（charged=1）时返还 1 次额度。"""
    conn = get_db()
    row = conn.execute(_sql("SELECT id, charged FROM reviews WHERE id = ? AND user_id = ?"),
                       (review_id, user_id)).fetchone()
    if not row:
        _close(conn)
        return False
    conn.execute(_sql("DELETE FROM findings WHERE review_id = ?"), (review_id,))
    conn.execute(_sql("DELETE FROM reviews WHERE id = ?"), (review_id,))
    if row["charged"]:
        # SQLite 与 PostgreSQL 通用的条件递减写法
        conn.execute(
            _sql("UPDATE users SET review_count = CASE WHEN review_count > 0 THEN review_count - 1 ELSE 0 END WHERE id = ?"),
            (user_id,))
    _close(conn)
    return True


# ---------------------------------------------------------------- 审计日志

def audit(user_id: int, username: str, action: str, detail: str = "") -> None:
    conn = get_db()
    conn.execute(_sql("INSERT INTO audit_log (user_id, username, action, detail) VALUES (?,?,?,?)"),
                 (user_id, username, action, detail[:2000]))
    _close(conn)


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)
