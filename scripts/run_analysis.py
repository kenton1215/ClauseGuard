# -*- coding: utf-8 -*-
"""CLI 审核管道测试：python scripts/run_analysis.py <合同文件> [--llm] [--commitment N]

用于在无 Streamlit 环境下验证解析→抽取→规则→评分全链路，也用于验收测试。"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.analyzer import ContractAnalyzer
from core.config import load_settings
from core.llm import LLMClient
from core.report import build_report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--llm", action="store_true", help="启用 LLM 通道")
    ap.add_argument("--commitment", type=int, default=0, help="投标承诺质保期(月)")
    ap.add_argument("--contract-type", default="购销合同")
    ap.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    path = Path(args.file)
    if not path.exists():
        print("文件不存在:", args.file)
        sys.exit(1)

    settings = load_settings()
    llm = LLMClient(settings["llm"]) if args.llm else None
    analyzer = ContractAnalyzer(settings, llm)
    result = analyzer.analyze(path.read_bytes(), path.name, args.contract_type,
                              commitment_warranty_months=args.commitment or None)

    if args.json:
        print(json.dumps({
            "file": path.name, "score": result.score, "level": result.risk_level,
            "hard_block": result.hard_block, "findings": [
                {"title": f["title"], "level": f["level"], "location": f["location"],
                 "evidence": f["evidence"][:80]} for f in result.findings],
            "facts": {k: v for k, v in result.facts.items() if not k.startswith("_")},
        }, ensure_ascii=False, indent=2))
    else:
        print(build_report(result))


if __name__ == "__main__":
    main()
