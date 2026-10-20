"""PDF → 페이지 / 근거 단위(source unit) 추출.

산출물
  data/processed/pages.jsonl  : PDF 한 페이지 = 한 줄 (청킹 전략의 공통 입력)
  data/processed/units.jsonl  : 정답 source_id 와 1:1 대응하는 근거 단위
      - 용어집 : 용어 1개        -> fsc-glossary-{dicId}
      - 표준약관: 조항 1개        -> kofia-terms-{119+파일번호}-{버전}-{조번호:03d}
      - 가이드북: 페이지 1개      -> fss-life-guide-pdf-{page}
      - 추가 PDF: 페이지 1개      -> {파일명}-pdf-{page}

청킹 전략은 무엇이든 자유지만, 각 청크는 자신이 걸쳐 있는 source_ids 를 반드시 들고 있어야
검색 평가(Hit@k, Recall@k)가 가능하다. -> src/chunking.py 의 attach_source_ids() 참고.

실행:  python -m src.ingest
"""
import json
import re
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "01_data"
OUT = ROOT / "data" / "processed"

# 검색 범위(scope) 정의: 기본 세트와 확장 세트의 검색 대상을 분리한다.
SCOPES = {
    "base": ["glossary", "terms"],                        # POS001~060, NEG001~040
    "life_guide": ["life_guide"],                        # POS061~080
    "additional": ["additional"],                        # ADD-*
    "all": ["glossary", "terms", "life_guide", "additional"],
}


def doc_type_of(path: Path) -> str:
    if path.name == "financial_glossary_readable.pdf":
        return "glossary"
    if path.name == "financial_life_guide.pdf":
        return "life_guide"
    if path.parent.name == "standard_terms":
        return "terms"
    return "additional"


def read_pages(path: Path):
    """페이지별 텍스트와 줄 정보(폰트 크기 포함)를 반환."""
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            lines = page.extract_text_lines() or []
            yield i, page.extract_text() or "", [
                {"text": l["text"], "size": round(l["chars"][0]["size"], 1)} for l in lines if l["chars"]
            ]


# ---------------------------------------------------------------- 용어집
def glossary_units(path: Path, pages):
    """폰트 11pt = 용어, 10pt = 정의. 마지막 부록의 'dicId=' 목록으로 용어 → ID 매핑."""
    full = "\n".join(t for _, t, _ in pages)
    # 부록: "용어 / 용어 설명 / https://...dicId=1697&..."
    # 같은 이름의 용어가 여러 번 나올 수 있으므로(예: '공매도' 2개) 이름 → [ID, ...] 를 등장 순서대로 소비
    appendix = [(m.group(1).strip(), int(m.group(2)))
                for m in re.finditer(r"^(.+?) / 용어 설명 /\s*\S*?dicId=(\d+)", full, flags=re.M)]
    id_map = {}
    for name, dic_id in appendix:
        id_map.setdefault(name, []).append(dic_id)

    units, cur = [], None
    for page_no, _, lines in pages:
        for ln in lines:
            if "/ 용어 설명 /" in ln["text"] or "dicId=" in ln["text"]:
                continue  # 부록(URL 목록)은 근거 본문이 아님
            if ln["size"] >= 10.5 and ln["size"] < 15:  # 용어 제목
                if cur:
                    units.append(cur)
                cur = {"term": ln["text"].strip(), "page": page_no, "lines": []}
            elif cur and ln["size"] < 10.5:
                cur["lines"].append(ln["text"])
    if cur:
        units.append(cur)

    # 본문 용어 수와 부록 수가 같으면 순서 그대로 대응(이름이 줄바꿈으로 깨진 경우까지 안전)
    by_order = len(units) == len(appendix)
    out, missing = [], []
    for idx, u in enumerate(units):
        if by_order:
            dic_id = appendix[idx][1]
        elif id_map.get(u["term"]):
            dic_id = id_map[u["term"]].pop(0)
        else:
            missing.append(u["term"])
            continue
        out.append({
            "source_id": f"fsc-glossary-{dic_id}",
            "doc_type": "glossary",
            "file": path.name,
            "pages": [u["page"]],
            "title": u["term"],
            "text": f"{u['term']}\n" + " ".join(u["lines"]),
        })
    if missing:
        print(f"  [warn] 용어집 ID 매핑 실패 {len(missing)}개: {missing[:5]}")
    return out


# ---------------------------------------------------------------- 표준약관
ARTICLE_RE = re.compile(r"^제(\d+)조(?:의(\d+))?\s*\(([^)]*)\)", flags=re.M)


def parse_source_list(full: str):
    """약관 PDF 끝의 '출처 목록'을 읽는다. 정답 ID 규칙은 이 목록에서 나온다(추측이 아니라 파일 근거):
         kofia-terms-{seq}-{historySeq}-{목록 순번:03d}
         seq·historySeq = 목록 URL 의 'seq=121&historySeq=1789'
         순번          = 목록에서 몇 번째 항목인지 (제10조의2 도 한 항목 → 조 번호와 순번이 어긋날 수 있음)
       반환: (seq, historySeq, {'제12조': 14, '제10조의2': 11, ...})"""
    i = full.find("출처 목록")
    if i < 0:
        return None
    appx = full[i:]
    flat = appx.replace("\n", "")                     # URL 이 줄바꿈으로 끊긴 경우 대비
    m = re.search(r"seq=(\d+)&historySeq=(\d+)", flat)
    order = {}
    for n, e in enumerate(re.finditer(r"^.+? / (제\d+조(?:의\d+)?)\s*\(", appx, flags=re.M), start=1):
        order.setdefault(e.group(1), n)
    return m.group(1), m.group(2), order


def terms_units(path: Path, pages):
    doc_title = pages[0][1].split("\n")[0].strip()

    # 페이지 경계를 기억하며 전체 텍스트를 이어 붙임
    full, offsets = "", []
    for page_no, text, _ in pages:
        offsets.append((len(full), page_no))
        full += text + "\n"

    def page_at(pos):
        return max(p for off, p in offsets if off <= pos)

    # 부칙 이후의 '제1조(시행일)' 등은 본문 조항이 아니므로 잘라낸다
    # 문서 끝의 '출처 목록'(조항별 URL 부록)도 본문이 아니므로 잘라낸다
    body_end = len(full)
    for pat in (r"^\s*부\s*칙", r"^\s*출처 목록"):
        m_appx = re.search(pat, full, flags=re.M)
        if m_appx:
            body_end = min(body_end, m_appx.start())

    src = parse_source_list(full)
    if src is None:
        raise ValueError(f"{path.name}: '출처 목록'이 없어 정답 ID 를 만들 수 없음")
    seq, hseq, order = src

    matches = [m for m in ARTICLE_RE.finditer(full) if m.start() < body_end]
    out, seen = [], set()
    for i, m in enumerate(matches):
        art_no, sub_no, art_title = int(m.group(1)), m.group(2), m.group(3)
        key = f"제{art_no}조" + (f"의{sub_no}" if sub_no else "")
        end = matches[i + 1].start() if i + 1 < len(matches) else body_end
        if key in seen:
            continue
        seen.add(key)
        if key not in order:
            print(f"  [warn] {path.name}: 본문 {key} 가 출처 목록에 없음 → 건너뜀")
            continue
        out.append({
            "source_id": f"kofia-terms-{seq}-{hseq}-{order[key]:03d}",
            "doc_type": "terms",
            "file": path.name,
            "pages": sorted({page_at(m.start()), page_at(max(m.start(), end - 1))}),
            "title": f"{doc_title} {key}({art_title})",
            "article": key,                     # 사람이 읽는 조항 번호 (ID 순번과 다를 수 있음)
            "text": full[m.start():end].strip(),
        })
    missing = set(order) - seen
    if missing:
        print(f"  [warn] {path.name}: 출처 목록에만 있고 본문에서 못 찾은 조항 {sorted(missing)[:5]}")
    return out


# ---------------------------------------------------------------- 페이지 단위 문서
def page_units(path: Path, pages, prefix: str, doc_type: str):
    return [{
        "source_id": f"{prefix}-pdf-{page_no}",
        "doc_type": doc_type,
        "file": path.name,
        "pages": [page_no],
        "title": f"{path.stem} p.{page_no}",
        "text": text,
    } for page_no, text, _ in pages]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    all_pages, all_units = [], []
    for path in sorted(DATA.rglob("*.pdf")):
        dt = doc_type_of(path)
        pages = list(read_pages(path))
        for page_no, text, _ in pages:
            all_pages.append({"doc_type": dt, "file": path.name, "page": page_no, "text": text})

        if dt == "glossary":
            units = glossary_units(path, pages)
        elif dt == "terms":
            units = terms_units(path, pages)
        elif dt == "life_guide":
            units = page_units(path, pages, "fss-life-guide", dt)
        else:
            units = page_units(path, pages, path.stem, dt)
        all_units.extend(units)
        print(f"{dt:10s} {len(pages):4d}p {len(units):4d} units  {path.name}")

    with open(OUT / "pages.jsonl", "w", encoding="utf-8") as f:
        for r in all_pages:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(OUT / "units.jsonl", "w", encoding="utf-8") as f:
        for r in all_units:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\npages={len(all_pages)} units={len(all_units)} -> {OUT}")


if __name__ == "__main__":
    main()
