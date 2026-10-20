"""정답 근거 ID 연결 (기획서 1장 '정답 근거 ID 규칙'). 출처: noviz ingest.py·check_sources.py.

근거 단위(source unit)
  용어집  용어 1개      fsc-glossary-{dicId}                       PDF 끝 '출처 목록' 순서로 용어↔dicId 연결
  약관    조항 1개      kofia-terms-{seq}-{historySeq}-{목록순번}   출처 목록 순서 (조 번호로 추측하지 않는다: 제10조의2)
  가이드북 쪽 1개        fss-life-guide-pdf-{N}                      N = PDF 쪽 순서
  추가    쪽 1개        {파일명}-pdf-{N}
용어집·약관은 단위 텍스트(청킹 입력)도 여기서 만든다: 원문 줄에서 쪽 번호·반복 머리말을 뺀 뒤 cid·특수 글리프를 정리.
"""
from __future__ import annotations

import re
from pathlib import Path

import pdfplumber

from src.loading.loader import PAGE_NO, clean_text, doc_type_of, norm_line

ARTICLE_RE = re.compile(r"^제(\d+)조(?:의(\d+))?\s*\(([^)]*)\)", flags=re.M)


def _filter_lines(text: str, repeated: set) -> str:
    out = []
    for ln in text.split("\n"):
        s = ln.strip()
        if s and (PAGE_NO.match(s) or norm_line(s) in repeated):
            continue
        out.append(ln)
    return clean_text("\n".join(out))


def parse_source_list(full: str):
    """약관 PDF 끝 '출처 목록' → (seq, historySeq, {'제12조': 14, '제10조의2': 11, ...}). 없으면 None."""
    i = full.find("출처 목록")
    if i < 0:
        return None
    appx = full[i:]
    flat = appx.replace("\n", "")
    m = re.search(r"seq=(\d+)&historySeq=(\d+)", flat)
    if not m:
        return None
    order = {}
    for n, e in enumerate(re.finditer(r"^.+? / (제\d+조(?:의\d+)?)\s*\(", appx, flags=re.M), start=1):
        order.setdefault(e.group(1), n)
    return m.group(1), m.group(2), order


def glossary_units(path: Path, pdf, repeated=frozenset()):
    """폰트 크기 10.5~15pt = 용어, 그보다 작으면 정의. 부록의 dicId 목록으로 ID 연결."""
    pages = []
    for i, page in enumerate(pdf.pages, start=1):
        lines = page.extract_text_lines() or []
        pages.append((i, page.extract_text() or "",
                      [{"text": l["text"], "size": round(l["chars"][0]["size"], 1)} for l in lines if l["chars"]]))
    full = "\n".join(t for _, t, _ in pages)
    appendix = [(m.group(1).strip(), int(m.group(2)))
                for m in re.finditer(r"^(.+?) / 용어 설명 /\s*\S*?dicId=(\d+)", full, flags=re.M)]
    id_map = {}
    for name, dic_id in appendix:
        id_map.setdefault(name, []).append(dic_id)
    raw, cur = [], None
    for page_no, _, lines in pages:
        for ln in lines:
            t = ln["text"].strip()
            if "/ 용어 설명 /" in ln["text"] or "dicId=" in ln["text"]:
                continue
            if PAGE_NO.match(t) or norm_line(t) in repeated:
                continue
            if 10.5 <= ln["size"] < 15:
                if cur:
                    raw.append(cur)
                cur = {"term": t, "page": page_no, "pages": {page_no}, "lines": []}
            elif cur and ln["size"] < 10.5:
                cur["lines"].append(t)
                cur["pages"].add(page_no)
    if cur:
        raw.append(cur)
    by_order = len(raw) == len(appendix)
    out, missing = [], []
    for idx, u in enumerate(raw):
        if by_order:
            dic_id = appendix[idx][1]
        elif id_map.get(u["term"]):
            dic_id = id_map[u["term"]].pop(0)
        else:
            missing.append(u["term"])
            continue
        out.append({"source_id": f"fsc-glossary-{dic_id}", "doc_type": "glossary", "file": path.name,
                    "pages": sorted(u["pages"]), "title": u["term"],
                    "text": clean_text(f"{u['term']}\n" + " ".join(u["lines"]))})
    return out, missing


def terms_units(path: Path, pdf, repeated=frozenset()):
    pages = [(i, page.extract_text() or "") for i, page in enumerate(pdf.pages, start=1)]
    doc_title = pages[0][1].split("\n")[0].strip()
    full, offsets = "", []
    for page_no, text in pages:
        offsets.append((len(full), page_no))
        full += text + "\n"

    def page_at(pos):
        return max(p for off, p in offsets if off <= pos)

    body_end = len(full)
    for pat in (r"^\s*부\s*칙", r"^\s*출처 목록"):     # 부칙·출처 목록은 본문 조항이 아니다
        m = re.search(pat, full, flags=re.M)
        if m:
            body_end = min(body_end, m.start())
    src = parse_source_list(full)
    if src is None:
        raise ValueError(f"{path.name}: '출처 목록'이 없어 정답 ID 를 만들 수 없음")
    seq, hseq, order = src
    matches = [m for m in ARTICLE_RE.finditer(full) if m.start() < body_end]
    out, seen, warn = [], set(), []
    for i, m in enumerate(matches):
        art_no, sub_no, art_title = int(m.group(1)), m.group(2), m.group(3)
        key = f"제{art_no}조" + (f"의{sub_no}" if sub_no else "")
        end = matches[i + 1].start() if i + 1 < len(matches) else body_end
        if key in seen:
            continue
        seen.add(key)
        if key not in order:
            warn.append(f"{path.name}: 본문 {key} 가 출처 목록에 없음 → 건너뜀")
            continue
        out.append({"source_id": f"kofia-terms-{seq}-{hseq}-{order[key]:03d}", "doc_type": "terms", "file": path.name,
                    "pages": sorted({page_at(m.start()), page_at(max(m.start(), end - 1))}),
                    "title": f"{key}({art_title})", "doc_title": doc_title, "article": key,
                    "article_no": art_no, "article_sub": int(sub_no) if sub_no else None,
                    "text": _filter_lines(full[m.start():end].strip(), repeated)})
    missing = set(order) - seen
    if missing:
        warn.append(f"{path.name}: 출처 목록에만 있고 본문에서 못 찾은 조항 {sorted(missing)[:5]}")
    return out, warn


def page_units(path: Path, n_pages: int, prefix: str, doc_type: str):
    return [{"source_id": f"{prefix}-pdf-{p}", "doc_type": doc_type, "file": path.name, "pages": [p],
             "title": f"{path.stem} p.{p}", "text": ""} for p in range(1, n_pages + 1)]


def build_units(pdf_paths, repeated_by_file: dict | None = None):
    """반환: (units, warnings). 쪽 단위 문서 text 는 비워 둔다(쪽 레코드가 본문)."""
    repeated_by_file = repeated_by_file or {}
    units, warns = [], []
    for path in pdf_paths:
        dt = doc_type_of(path)
        rep = repeated_by_file.get(path.name, frozenset())
        with pdfplumber.open(path) as pdf:
            if dt == "glossary":
                u, miss = glossary_units(path, pdf, rep)
                if miss:
                    warns.append(f"{path.name}: 용어 ID 연결 실패 {len(miss)}개 {miss[:5]}")
            elif dt == "terms":
                u, w = terms_units(path, pdf, rep)
                warns += w
            elif dt == "life_guide":
                u = page_units(path, len(pdf.pages), "fss-life-guide", dt)
            else:
                u = page_units(path, len(pdf.pages), path.stem, dt)
        units += u
    return units, warns
