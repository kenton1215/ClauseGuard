# -*- coding: utf-8 -*-
"""将下载的法条 HTML 解析为逐条结构化 JSON。

用法:
    python scripts/build_law_json.py <input.html> <law_name> [--out output.json] [--chapter-map ...]

输出格式:
{
  "law_name": "...", "source": "...", "updated_at": "...",
  "chapters": {"1": "第一章 总则", ...},   # 条号 -> 所属章节标题
  "articles": [{"no": 463, "article": "第四百六十三条", "text": "本编调整...", "chapter": "第一分编 通则"}]
}
"""
import argparse
import html as html_mod
import json
import re
import sys
from datetime import date
from pathlib import Path

CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def cn_num_to_int(s: str) -> int:
    """中文数字转 int，如 '一千二百六十' -> 1260。"""
    total, section, num = 0, 0, 0
    for ch in s:
        if ch in CN_DIGITS:
            num = CN_DIGITS[ch]
        elif ch in CN_UNITS:
            unit = CN_UNITS[ch]
            if unit == 10000:
                section = (section + num) * unit
                total += section
                section, num = 0, 0
            else:
                section += (num if num else 1) * unit
                num = 0
        else:
            return -1
    return total + section + num


def parse_article_no(token: str):
    """'第一千二百六十条' / '第463条' / '第１５条' -> int 条号，失败返回 None。"""
    m = re.match(r"^第([0-9０-９]+)条", token)
    if m:
        return int(m.group(1).translate(str.maketrans("０１２３４５６７８９", "0123456789")))
    m = re.match(r"^第([零〇一二两三四五六七八九十百千万]+)条", token)
    if m:
        return cn_num_to_int(m.group(1))
    return None


def strip_tags(raw: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", "", raw, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
    text = re.sub(r"<!--[\s\S]*?-->", "", text)
    # 将 </p> <br> <tr> 等块级标签替换为换行，保持段落结构
    text = re.sub(r"</(p|div|tr|h\d|li)>", "\n", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    text = text.replace("　", " ")
    return text


ARTICLE_RE = re.compile(r"第[零〇一二两三四五六七八九十百千万0-9０-９]+条")
CHAPTER_RE = re.compile(r"^第[零〇一二两三四五六七八九十百千]+[编章节](?:　| |\S{0,30})")


def parse_law_html(raw_html: str, law_name: str, source: str):
    text = strip_tags(raw_html)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    lines = [re.sub(r"\s+", " ", ln) for ln in lines]

    articles = []
    chapters = {}
    current_chapter = ""

    for ln in lines:
        m = CHAPTER_RE.match(ln)
        if m and not ARTICLE_RE.match(ln):
            current_chapter = ln[:40]
            continue
        pos = 0
        while True:
            m = ARTICLE_RE.search(ln, pos)
            if not m:
                break
            no = parse_article_no(m.group(0))
            if no is None:
                pos = m.end()
                continue
            body_start = m.end()
            nxt = ARTICLE_RE.search(ln, body_start)
            body = ln[body_start:nxt.start() if nxt else len(ln)].strip()
            if no in chapters:
                chapters[no] = current_chapter or chapters[no]
            else:
                chapters[no] = current_chapter
            # 同一行多条（如"第一条…第二条…"）分别成条
            articles.append({
                "no": no,
                "article": f"第{no}条",
                "text": body,
                "chapter": current_chapter,
            })
            if not nxt:
                break
            pos = nxt.start()

    # 去重（保留正文非空或最长者）
    best = {}
    for a in articles:
        key = a["no"]
        if key not in best or len(a["text"]) > len(best[key]["text"]):
            best[key] = a
    articles = [best[k] for k in sorted(best)]
    return {"law_name": law_name, "source": source,
            "updated_at": date.today().isoformat(),
            "article_count": len(articles), "articles": articles}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("law_name")
    ap.add_argument("--source", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    raw = Path(args.input).read_text(encoding="utf-8", errors="ignore")
    data = parse_law_html(raw, args.law_name, args.source)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: {data['law_name']} 共 {data['article_count']} 条 -> {out}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
