# -*- coding: utf-8 -*-
"""知识库检索层（需求 R7 的 P0 实现）。

零依赖检索：关键词倒排索引 + 字符 bigram 相似度打分。
P1 升级路径：接入向量嵌入（M3E/bge-small-zh）替换相似度打分，接口不变。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from core.config import KNOWLEDGE_DIR

LAW_FILES = [
    ("民法典", KNOWLEDGE_DIR / "legal" / "民法典_逐条.json"),
    ("公司法", KNOWLEDGE_DIR / "legal" / "公司法2023_逐条.json"),
    ("民诉法", KNOWLEDGE_DIR / "legal" / "民诉法2023_逐条.json"),
]
CLAUSE_LIB_PATH = KNOWLEDGE_DIR / "standards" / "clause_library.json"
CASE_LIB_PATH = KNOWLEDGE_DIR / "cases" / "case_library.json"

# 条文内【条目名称】括号内容（民法典官方文本自带，如【损害赔偿范围】）
BRACKET_RE = re.compile(r"【([^】]{2,20})】")

# 合同审核高频概念 → 法条检索关键词
CONCEPT_KEYWORDS = {
    "违约责任": ["违约", "赔偿", "违约金", "责任"],
    "违约金": ["违约金", "违约", "损失"],
    "损害赔偿": ["损害赔偿", "损失", "赔偿"],
    "不可抗力": ["不可抗力", "免责"],
    "解除": ["解除", "终止"],
    "管辖": ["管辖", "协议管辖", "诉讼"],
    "仲裁": ["仲裁", "争议"],
    "争议解决": ["争议", "仲裁", "管辖", "诉讼"],
    "质保": ["检验", "质量", "瑕疵", "保修"],
    "付款": ["价款", "支付", "履行"],
    "预付款": ["价款", "支付"],
    "知识产权": ["知识产权", "技术", "成果", "专利"],
    "保密": ["保密", "秘密", "商业秘密"],
    "格式条款": ["格式条款", "不公平"],
    "担保": ["担保", "保证"],
    "主体资格": ["主体", "法人", "资格", "登记"],
    "签章": ["成立", "签字", "盖章"],
    "倒签": ["生效", "成立", "履行"],
    "自动续约": ["期限", "解除", "续"],
    "送达": ["通知", "送达"],
}


class LawIndex:
    def __init__(self):
        self.articles: list[dict] = []
        self._kw_index: dict[str, set[int]] = {}
        for law_name, path in LAW_FILES:
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for a in data.get("articles", []):
                record = {
                    "law": law_name, "no": a.get("no"), "article": a.get("article", ""),
                    "text": a.get("text", ""), "chapter": a.get("chapter", ""),
                }
                idx = len(self.articles)
                self.articles.append(record)
                for kw in self._keywords_of(record):
                    self._kw_index.setdefault(kw, set()).add(idx)

    @staticmethod
    def _keywords_of(record: dict) -> set[str]:
        kws = set()
        m = BRACKET_RE.search(record["text"])
        if m:
            kws.add(m.group(1))
        for kw in ("违约金", "违约", "赔偿", "不可抗力", "解除", "管辖", "仲裁",
                   "保密", "知识产权", "担保", "格式条款", "定金", "检验", "质量",
                   "通知", "价款", "支付", "技术", "租赁", "承揽", "运输", "仓储",
                   "委托", "保管", "保证", "合伙", "借款", "买卖", "赠与", "建设工程"):
            if kw in record["text"]:
                kws.add(kw)
        return kws

    def retrieve(self, query: str, top_k: int = 5) -> list[dict]:
        """按概念关键词 + 字符 bigram 相似度检索法条。"""
        query_kws: set[str] = set()
        for concept, kws in CONCEPT_KEYWORDS.items():
            if concept in query:
                query_kws.update(kws)
        for kw in self._keywords_of({"text": query, "chapter": ""}):
            query_kws.add(kw)
        if not query_kws:
            query_kws = {"违约", "合同"}

        scores: dict[int, float] = {}
        for kw in query_kws:
            for idx in self._kw_index.get(kw, ()):
                scores[idx] = scores.get(idx, 0.0) + 1.0

        # 字符 bigram 相似度兜底
        q_grams = _bigrams(query)
        if q_grams:
            for i, a in enumerate(self.articles):
                overlap = len(q_grams & _bigrams(a["text"][:200]))
                scores[i] = scores.get(i, 0.0) + overlap * 0.05

        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:top_k]
        return [self.articles[i] for i, _ in ranked]


def _bigrams(text: str) -> set[str]:
    text = re.sub(r"[\s，。；：,.、（）()【】]", "", text)
    return {text[i:i + 2] for i in range(max(0, len(text) - 1))}


class ClauseLibrary:
    """标准条款库检索：按条款类型/关键词匹配。"""

    def __init__(self, path: Path | None = None):
        p = path or CLAUSE_LIB_PATH
        self.items: list[dict] = []
        if p.exists():
            try:
                self.items = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.items = []

    def find(self, query: str, top_k: int = 2) -> list[dict]:
        scored = []
        q_grams = _bigrams(query)
        for item in self.items:
            text = str(item.get("type", "")) + str(item.get("preferred_text", ""))
            score = 0.0
            if q_grams:
                score = len(q_grams & _bigrams(text)) * 0.1
            for kw in (item.get("type", ""), *[c for c in ["付款", "质保", "违约", "保密", "知识产权", "争议", "不可抗力", "解除", "终止", "送达"] if c in query]):
                if kw and kw in query:
                    score += 1.0
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda kv: -kv[0])
        return [it for _, it in scored[:top_k]]


class CaseLibrary:
    """历史案例库检索（条款-风险标注对，用于相似案例参考）。"""

    def __init__(self, path: Path | None = None):
        p = path or CASE_LIB_PATH
        self.items: list[dict] = []
        if p.exists():
            try:
                self.items = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.items = []

    def find_similar(self, issue_title: str, top_k: int = 2) -> list[dict]:
        scored = []
        q_grams = _bigrams(issue_title)
        for item in self.items:
            text = str(item.get("issue", "")) + str(item.get("contract_type", ""))
            score = len(q_grams & _bigrams(text)) if q_grams else 0
            for kw in ["质保", "知识产权", "付款", "违约", "保密", "争议", "倒签", "无限责任", "管辖"]:
                if kw in issue_title and kw in text:
                    score += 2.0
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda kv: -kv[0])
        return [it for _, it in scored[:top_k]]
