# -*- coding: utf-8 -*-
"""数据库查看工具（本地 SQLite 与云端 PostgreSQL 通用，敏感字段脱敏）。

用法：
    python scripts/view_db.py               # 总览：各表行数
    python scripts/view_db.py users         # 用户列表（密码哈希/Key 脱敏）
    python scripts/view_db.py reviews [N]   # 最近 N 条审核记录（默认10）
    python scripts/view_db.py findings <id> # 某次审核的风险批注
    python scripts/view_db.py audit [N]     # 最近 N 条审计日志（默认20）
    python scripts/view_db.py sql "<SQL>"   # 只读执行自定义 SQL（高级）
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import db  # noqa: E402

W = 110


def _print_rows(title: str, columns: list[str], rows: list[dict], mask: dict | None = None) -> None:
    print("=" * W)
    print(f" {title}")
    print("=" * W)
    if not rows:
        print(" （无数据）")
        return
    mask = mask or {}
    print(" | ".join(c for c in columns))
    print("-" * W)
    for r in rows:
        vals = []
        for c in columns:
            v = str(r.get(c, ""))
            if c in mask and v:
                v = mask[c](v)
            vals.append(v[:48] + ("…" if len(v) > 48 else ""))
        print(" | ".join(vals))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    db.init_db()
    conn = db.get_db()

    def q(sql, *args):
        cur = conn.execute(db._sql(sql), args) if args else conn.execute(db._sql(sql))
        return [dict(r) if not isinstance(r, dict) else r for r in cur.fetchall()]

    cmd = sys.argv[1] if len(sys.argv) > 1 else ""

    if cmd == "users":
        rows = q("SELECT id, username, role, quota_total, review_count, llm_mode, llm_api_key, created_at FROM users ORDER BY id")
        _print_rows("用户列表", ["id", "username", "role", "quota_total", "review_count", "llm_mode", "llm_api_key", "created_at"], rows,
                    mask={"llm_api_key": lambda v: (v[:14] + "…") if v else ""})

    elif cmd == "reviews":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        rows = q("SELECT id, file_name, contract_type, contract_no, party_a, party_b, score, risk_level, hard_block, issue_count, charged, created_at FROM reviews ORDER BY id DESC LIMIT ?", n)
        _print_rows("最近审核记录", ["id", "file_name", "contract_type", "score", "risk_level", "hard_block", "issue_count", "charged", "created_at"], rows)

    elif cmd == "findings":
        rid = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        rows = q("SELECT seq, title, level, hard_block, location, status, confidence FROM findings WHERE review_id = ? ORDER BY seq", rid)
        _print_rows(f"审核 #{rid} 的风险批注", ["seq", "title", "level", "hard_block", "location", "status", "confidence"], rows)

    elif cmd == "audit":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        rows = q("SELECT id, username, action, detail, created_at FROM audit_log ORDER BY id DESC LIMIT ?", n)
        _print_rows("审计日志", ["id", "username", "action", "detail", "created_at"], rows)

    elif cmd == "sql":
        sql_text = sys.argv[2]
        if not sql_text.strip().lower().startswith("select"):
            print("仅允许 SELECT 只读查询。")
            sys.exit(1)
        rows = q(sql_text)
        cols = list(rows[0].keys()) if rows else []
        _print_rows("自定义查询结果", cols, rows)

    else:
        tables = ["users", "reviews", "findings", "audit_log", "paid_unlocks"]
        counts = [(t, q(f"SELECT COUNT(*) c FROM {t}")[0]["c"]) for t in tables]
        mode = "PostgreSQL（外部持久化）" if db._is_pg() and not db.is_degraded() else \
               "SQLite（本地）" if not db._is_pg() else "SQLite（降级临时模式）"
        print("=" * W)
        print(f" 企业合同审核智能体 · 数据库总览")
        print(f" 存储模式：{mode}")
        print("=" * W)
        for t, c in counts:
            print(f"   {t:<16} {c} 行")
        print("-" * W)
        print(" 用法：python scripts/view_db.py users|reviews|findings|audit|sql")


if __name__ == "__main__":
    main()
