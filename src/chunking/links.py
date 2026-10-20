"""청크 사이 링크: prev/next, 조항 참조('제7조에 따라'), 그림·표 참조('그림 Ⅲ-9'). 출처: noviz parent_child.py.

links = {"prev": id|None, "next": id|None,
         "article_refs": [{"label": "제7조", "target": chunk_id}],
         "figure_refs":  [{"label": "그림III-9", "target": chunk_id}]}
Agent 가 근거가 부족할 때 follow_ref 로 따라간다.
"""
from __future__ import annotations

import re
from collections import defaultdict

FIG_LABEL = re.compile(r"(그림|참고|표)\s*\]?\s*([IVXⅠ-Ⅹ]+\s*[-.]\s*\d+|\d+\s*-\s*\d+|\d+)")
ART_REF = re.compile(r"제(\d+)조(?:의(\d+))?")
EXTERNAL_HINT = re.compile(r"(법|령|규칙|규정|」|시행)\s*(같은 법\s*)?$")
ROMAN = {"Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "Ⅳ": "IV", "Ⅴ": "V", "Ⅵ": "VI", "Ⅶ": "VII", "Ⅷ": "VIII", "Ⅸ": "IX", "Ⅹ": "X"}


def label_key(kind, num):
    num = re.sub(r"\s+", "", num).replace(".", "-")
    for k, v in ROMAN.items():
        num = num.replace(k, v)
    return f"{kind}{num}"


def labels_in(text):
    return {label_key(m.group(1), m.group(2)) for m in FIG_LABEL.finditer(text or "")}


def article_refs_in(text, own_no, own_sub):
    """같은 약관 안의 조항 번호 [(no, sub)]. 줄 첫머리의 '제N조(' 는 제목이므로 제외, 법령 조항은 제외."""
    out = []
    for m in ART_REF.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        if m.start() == line_start and text[m.end():m.end() + 1] == "(":
            continue
        if EXTERNAL_HINT.search(text[max(0, m.start() - 14):m.start()]):
            continue
        key = (int(m.group(1)), int(m.group(2)) if m.group(2) else None)
        if key != (own_no, own_sub) and key not in out:
            out.append(key)
    return out


def add_links(chunks: list[dict]) -> None:
    by_file = defaultdict(list)
    for c in chunks:
        by_file[c["file"]].append(c)
    for seq in by_file.values():
        for a, b in zip(seq, seq[1:]):
            a["links"]["next"], b["links"]["prev"] = b["chunk_id"], a["chunk_id"]
    # 조항 참조 (약관)
    first_of = {}
    for c in chunks:
        if c.get("article_no") is not None and c["chunk_index"] == 0:
            first_of[(c["file"], c["article_no"], c.get("article_sub"))] = c["chunk_id"]
    for c in chunks:
        if c["doc_type"] != "terms" or c.get("article_no") is None:
            continue
        for no, sub in article_refs_in(c["text"], c["article_no"], c.get("article_sub")):
            tid = first_of.get((c["file"], no, sub))
            if tid:
                c["links"]["article_refs"].append({"label": f"제{no}조" + (f"의{sub}" if sub else ""), "target": tid})
    # 그림·표 참조 (가이드북·추가). 대상 라벨은 캡션과 라벨로 시작하는 첫 줄에서만 읽는다
    targets = defaultdict(dict)
    for c in chunks:
        if c["content_type"] in ("figure", "table"):
            body = re.sub(r"^\[(그림|표)\]\s*", "", c["text"]).split("\n")
            first = next((ln for ln in body[:3] if FIG_LABEL.match(ln.strip(" [<"))), "")
            for key in labels_in((c.get("caption") or "") + " " + first):
                targets[c["file"]].setdefault(key, c["chunk_id"])
    for c in chunks:
        if c["content_type"] != "text":
            continue
        for key in sorted(labels_in(c["text"])):
            tid = targets[c["file"]].get(key)
            if tid and tid != c["chunk_id"]:
                c["links"]["figure_refs"].append({"label": key, "target": tid})
