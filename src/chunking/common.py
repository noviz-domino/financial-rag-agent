"""청킹 공통: 문서 제목, 청크 만들기, 재귀 분할기, 근거 ID 연결 도우미."""
from __future__ import annotations

import re

DOC_TITLES = {
    "financial_glossary_readable.pdf": "금융용어집",
    "financial_life_guide.pdf": "금융생활 가이드북(사회 첫걸음부터 똑똑하게)",
    "housing_pension_2026.pdf": "주택연금 백문백답(2026)",
    "housing_policy_finance_2025.pdf": "우리나라 주택 정책금융 현황과 평가(2025)",
    "insurance_terms_example_2020.pdf": "보험약관 예시(2020)",
    "policy_finance_debt_adjustment_2026.pdf": "정책서민금융·채무조정 평가(2026)",
    "retirement_pension_2025.pdf": "2025년 퇴직연금 적립·운용 현황",
    "self_employed_debt_2025.pdf": "자영업 가구 채무상환능력 평가(2025)",
}


def doc_title(file: str) -> str:
    if file in DOC_TITLES:
        return DOC_TITLES[file]
    return re.sub(r"^\d+_", "", file[:-4])      # '06_연금저축계좌설정약관.pdf' → '연금저축계좌설정약관'


def slug(file: str) -> str:
    if file == "financial_glossary_readable.pdf":
        return "glossary"
    if file == "financial_life_guide.pdf":
        return "lifeguide"
    m = re.match(r"(\d+)_", file)
    return f"terms{m.group(1)}" if m else file[:-4]


def page_source_id(file: str, page: int) -> str:
    stem = "fss-life-guide" if file == "financial_life_guide.pdf" else file[:-4]
    return f"{stem}-pdf-{page}"


def make_chunk(cid, file, doc_type, pages, section, text, *, content_type="text", source="text_layer", source_ids=(),
               parent_id=None, half=None, chunk_index=0, strategy="structural", **extra) -> dict:
    pages = sorted(set(pages))
    text = text.strip()
    return {"chunk_id": cid, "text": text, "embed_text": f"[{section}]\n{text}" if section else text,
            "file": file, "doc_type": doc_type, "page": pages[0] if pages else None, "pages": pages, "half": half,
            "section": section, "content_type": content_type, "source": source,
            "source_ids": sorted(set(source_ids)), "parent_id": parent_id,
            "links": {"prev": None, "next": None, "article_refs": [], "figure_refs": []},
            "chunk_index": chunk_index, "strategy": strategy, "doc_title": doc_title(file), "n_chars": len(text),
            **extra}


# ------------------------------------------------------------------ 재귀 분할 (RecursiveCharacterTextSplitter 와 같은 방식)
def _segments(text, start, end, size, seps):
    """[start, end) 를 구분자(큰 것부터)로 쪼갠 size 이하 조각들의 span 목록. 구분자는 앞 조각 끝에 붙여 span 이 이어진다."""
    if end - start <= size:
        return [(start, end)]
    for i, sep in enumerate(seps):
        if sep == "":
            break
        if sep in text[start:end]:
            out, pos = [], start
            while pos < end:
                j = text.find(sep, pos, end)
                nxt = end if j < 0 else j + len(sep)
                out += _segments(text, pos, nxt, size, seps[i + 1:])
                pos = nxt
            return out
    return [(k, min(k + size, end)) for k in range(start, end, size)]


_SNAP = 16          # 겹침 시작점을 공백·문장부호 쪽으로 당겨 올 수 있는 최대 글자 수
_SNAP_CHARS = " \n\t.,;:!?)]}。·"


def _snap_back(text, pos, lo):
    """pos 에서 앞(lo 까지)으로 가며 공백·문장부호 바로 뒤를 찾는다. 없으면 pos 그대로."""
    for k in range(pos, max(lo, pos - _SNAP) - 1, -1):
        if k > 0 and text[k - 1] in _SNAP_CHARS:
            return k
    return pos


def recursive_split(text: str, size: int = 500, overlap: int = 50, seps=("\n\n", "\n", " ", "")):
    """반환: [(조각 글, 시작 위치)]. 조각은 원문의 연속 구간이라 위치로 쪽·근거 ID 를 연결할 수 있다.

    겹침은 글자 단위로 실제 적용한다: 먼저 (size - overlap - 여유) 크기로 겹침 없이 묶고, 다음 조각의 시작을
    앞 조각 끝에서 overlap 글자 앞으로 당긴다(줄 중간이어도 됨). 시작점은 가까운 공백·문장부호 뒤로 맞춘다.
    그래서 이웃 조각은 최소 overlap 글자를 공유하고 조각 길이는 size 이하다."""
    overlap = max(0, min(overlap, size // 2))
    slack = _SNAP if size - overlap - _SNAP >= size // 3 else 0
    budget = size - overlap - slack
    segs = [s for s in _segments(text, 0, len(text), budget, list(seps)) if s[1] > s[0]]
    spans, cur_a, cur_b = [], None, None
    for a, b in segs:
        if cur_a is not None and b - cur_a > budget:
            spans.append((cur_a, cur_b))
            cur_a = None
        if cur_a is None:
            cur_a = a
        cur_b = b
    if cur_a is not None:
        spans.append((cur_a, cur_b))
    out, prev_end = [], None
    for a, b in spans:
        start = a
        if prev_end is not None and overlap:
            start = _snap_back(text, max(0, prev_end - overlap), max(0, prev_end - overlap - slack))
            start = min(start, a)
        piece = text[start:b]
        if piece.strip():
            out.append((piece, start))
        prev_end = b
    return out


def overlaps(spans, s, e, min_chars=30):
    """[s, e) 와 min_chars 이상(짧은 단위는 절반 이상) 겹치는 span 의 값."""
    out = []
    for a, b, v in spans:
        ov = min(b, e) - max(a, s)
        if ov > 0 and ov >= min(min_chars, (b - a) * 0.5):
            out.append(v)
    return out
