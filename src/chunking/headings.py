"""글꼴 크기로 제목 줄 찾기 (가이드북·추가 PDF 용). 출처: jjh chunking.py heading_map (글꼴 크기·굵기 기반).

반환: {쪽: {공백 없는 글: 단계(1~3)}}. 쪽 레코드의 줄과 '공백 없는 글'로 맞춰 본다 (반쪽 레코드도 같은 쪽 번호).
"""
from __future__ import annotations

import re
from collections import Counter

import pdfplumber

BULLET = re.compile(r"^(?:[■◦○●◆▶▷·※•□▪◇\-*]|[①-⑳]|\(\d+\)\s|\d+[.)]\s|[가-힣][.)]\s|[-])")
NOT_HEAD = re.compile(r"^(?:\[표|\[그림|<출처|자료\s*[:：]|주\s*[:：)]|출처\s*[:：]|※)")


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def heading_map(pdf_path) -> dict:
    rows, sizes, long_sizes = [], Counter(), Counter()
    with pdfplumber.open(str(pdf_path)) as pdf:
        for pg in pdf.pages:
            pr = []
            for ln in pg.extract_text_lines(return_chars=True, strip=True):
                cs = [c for c in ln["chars"] if c["text"].strip()]
                if not cs:
                    continue
                sz = Counter(round(c["size"] * 2) / 2 for c in cs).most_common(1)[0][0]
                fn = Counter(c["fontname"].split("+")[-1] for c in cs).most_common(1)[0][0]
                pr.append((sz, fn, ln["text"].strip()))
                sizes[sz] += len(cs)
                if len(norm(ln["text"])) >= 25:
                    long_sizes[sz] += len(cs)
            rows.append(pr)
    if not sizes:
        return {}
    body = max(sizes.most_common(1)[0][0], (long_sizes or sizes).most_common(1)[0][0])
    cands = []
    for pno, pr in enumerate(rows, 1):
        for sz, fn, t in pr:
            n = norm(t)
            big = sz >= body + 0.9
            boldish = sz >= body + 0.4 and re.search(r"Bold|Black|Heavy|EB|Medium", fn) and len(n) <= 40
            if not (big or boldish) or len(n) < 2 or len(n) > 70 or re.fullmatch(r"[\d\W]+", n):
                continue
            if BULLET.match(t) and not re.match(r"^\d+[.)]\s|^[가-힣][.)]\s", t) or NOT_HEAD.match(t) or t.endswith("다."):
                continue
            cands.append((pno, sz, n))
    fkey = lambda sz, n: (sz, re.sub(r"[\d｢｣「」\[\]]+", "", n))
    freq = Counter(fkey(sz, n) for _, sz, n in cands)
    cands = [c for c in cands if freq[fkey(c[1], c[2])] < 3]           # 쪽마다 반복되는 머리글 제외
    size_pages, size_count = {}, Counter()
    for pno, sz, _ in cands:
        size_pages.setdefault(sz, set()).add(pno)
        size_count[sz] += 1
    ok = lambda sz: size_count[sz] >= 2 and not (len(rows) >= 10 and size_pages[sz] <= {1, 2, 3})
    levels = {sz: i + 1 for i, sz in enumerate(sorted([s for s in size_count if ok(s)], reverse=True)[:3])}
    out = {}
    for pno, sz, n in cands:
        if sz in levels:
            out.setdefault(pno, {})[n] = levels[sz]
            m = re.match(r"^\d{1,3}(?=\D)(.+)$", n)          # '15 부모님이…' 처럼 번호와 제목이 한 줄인 경우
            if m and len(m.group(1)) >= 3:
                out[pno].setdefault(m.group(1), levels[sz])
    return out
