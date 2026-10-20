"""청킹 전략 모음. 결과: data/chunks/{strategy_name}.jsonl

입력
  - 용어집·약관 : data/processed/pages.jsonl (1단 문서라 기본 추출로 충분) + units.jsonl (용어/조항 단위)
  - 가이드북·추가: data/processed/pages_v2.jsonl (레이아웃 로더 결과, src/loader.py)

전략
  recursive  : 문서 전체를 이어 붙여 RecursiveCharacterTextSplitter (size/overlap). 기준선.
  structural : 용어집 = 용어 1개, 약관 = 조항 1개(길면 ①②… 항 단위로 분할),
               가이드북·추가 = 영역(반쪽 페이지)을 제목·글머리표 경계로 나눈 뒤 max_chars 까지 묶음
  --title    : embed_text 앞에 [문서명 > 상위 제목] 을 붙임 (text 는 그대로 → '문맥 보존' 실험에서 한 요소만 변경)

청크 스키마 (docs/회의_선택지_심화.md 3장)
  chunk_id, strategy, doc_type, file, doc_title, pages, source_ids, title, text, embed_text, n_chars

실행
  python -m src.chunking --strategy recursive --size 500 --overlap 50
  python -m src.chunking --strategy structural --max-chars 1200
  python -m src.chunking --strategy structural --title
  python -m src.chunking --strategy structural --figures   # 그림을 그림별 청크로 (src/figures.py 먼저 실행)
  python -m src.chunking --strategy parentchild --child-max 400   # Parent-Child + 링크 (src/parent_child.py)
  python -m src.chunking --strategy structural --pages pages_p4l_layout.jsonl   # 로더 비교 → structural_1200_p4l_layout
"""
import argparse
import json
import re
from pathlib import Path

from langchain_text_splitters import RecursiveCharacterTextSplitter

from src.loader import render_text

ROOT = Path(__file__).resolve().parent.parent
P = ROOT / "data" / "processed"
OUT = ROOT / "data" / "chunks"
# 가이드북·추가 PDF 입력. --pages 로 다른 로더 결과(예: pages_p4l_layout.jsonl)를 넣으면 로더만 바꾼 비교가 된다
PAGES_FILE = "pages_v2.jsonl"
NAME_SUFFIX = ""

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
# 가이드북·추가 PDF 에서 '새 단락이 시작되는' 줄
HEADING = re.compile(
    r"^(#{1,6}\s|\d+단계|[IVXⅠ-Ⅹ]+[.\s]|\d{1,2}\s+[가-힣]|[①-⑳]|[□■▶◆●○◦]|\[표|\[그림|<그림|그림\s|표\s*[IVXⅠ-Ⅹ\d]|"
    r"참고\s*\d|Q\d*[.\s]|\d+\.\s|[가-하]\.\s)")
ARTICLE_PARA = re.compile(r"(?=[①-⑳])")


def doc_title(file):
    if file in DOC_TITLES:
        return DOC_TITLES[file]
    return re.sub(r"^\d+_", "", file[:-4])  # 약관: '06_연금저축계좌설정약관.pdf' → '연금저축계좌설정약관'


def page_sid(file, page):
    stem = "fss-life-guide" if file == "financial_life_guide.pdf" else file[:-4]
    return f"{stem}-pdf-{page}"


def slug(file):
    if file == "financial_glossary_readable.pdf":
        return "glossary"
    if file == "financial_life_guide.pdf":
        return "lifeguide"
    m = re.match(r"(\d+)_", file)
    return f"terms{m.group(1)}" if m else file[:-4]


def load_jsonl(p):
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


CHUNK_META_DEFAULTS = {
    "half": None,            # 펼침면이면 "L"/"R"
    "printed_page": None,    # 인쇄된 쪽번호 (답안 locator 의 '인쇄 18쪽' 대응)
    "chunk_index": 0,        # 같은 페이지(또는 같은 조항) 안에서의 순서
    "content_type": "text",  # text | table | figure
    "figure_kind": None,     # raster | vector_chart | lossy_table
    "source": "text_layer",  # text_layer | pdf_table | gemini:<모델> | ocr | caption+text_layer
    "bbox": None, "image_path": None, "caption": None,
    "num_check": None,       # 그림 Markdown 숫자 중 PDF 텍스트에 실제로 있는 비율 (환각 점검)
}


def make_chunk(strategy, file, doc_type, pages, source_ids, title, text, cid, add_title, **meta):
    dtitle = doc_title(file)
    head = f"[{dtitle} > {title}]\n" if add_title and title else (f"[{dtitle}]\n" if add_title else "")
    pages = sorted(set(pages))
    return {
        "chunk_id": cid, "strategy": strategy, "doc_id": file[:-4], "doc_type": doc_type, "file": file,
        "doc_title": dtitle, "page": pages[0] if pages else None, "pages": pages,
        **{**CHUNK_META_DEFAULTS, **meta},
        "source_ids": sorted(set(source_ids)), "title": title,
        "text": text.strip(), "embed_text": head + text.strip(), "n_chars": len(text.strip()),
    }


# ------------------------------------------------------------------ 문서 원문 + 근거 단위 위치
def build_documents():
    """파일별로 (전체 텍스트, [(start, end, page)] 페이지 구간, [(start, end, source_id)] 근거 구간)."""
    docs = {}
    units = load_jsonl(P / "units.jsonl")
    # 용어집·약관: 기존 추출 페이지를 이어 붙이고, 근거 단위(용어/조항)의 시작 위치를 순서대로 찾는다
    for r in load_jsonl(P / "pages.jsonl"):
        if r["doc_type"] not in ("glossary", "terms"):
            continue
        d = docs.setdefault(r["file"], {"doc_type": r["doc_type"], "text": "", "pages": [], "units": []})
        s = len(d["text"])
        d["text"] += r["text"] + "\n"
        d["pages"].append((s, len(d["text"]), r["page"]))
    # 부록(용어집 dicId URL 목록, 약관 '출처 목록')은 본문이 아니므로 잘라낸다.
    # structural 은 units(부록 제외)를 쓰므로, recursive 도 같은 본문만 받아야 공정한 비교가 됨
    for d in docs.values():
        m = re.search(r"^.*( / 용어 설명 / |^\s*출처 목록)", d["text"], flags=re.M)
        if m:
            cut = m.start()
            d["text"] = d["text"][:cut]
            d["pages"] = [(s, min(e, cut), p) for s, e, p in d["pages"] if s < cut]
    for file, d in docs.items():
        us = [u for u in units if u["file"] == file]
        pos, anchors = 0, []
        for u in us:
            anchor = u["title"] if d["doc_type"] == "glossary" else u["text"][:25]
            i = d["text"].find(anchor, pos)
            if i < 0:
                continue
            anchors.append((i, u["source_id"]))
            pos = i + 1
        for k, (i, sid) in enumerate(anchors):
            end = anchors[k + 1][0] if k + 1 < len(anchors) else len(d["text"])
            d["units"].append((i, end, sid))
    # 가이드북·추가: 레이아웃 로더 결과. 근거 단위 = 페이지
    for r in load_jsonl(P / PAGES_FILE):
        if r["doc_type"] not in ("life_guide", "additional"):
            continue
        d = docs.setdefault(r["file"], {"doc_type": r["doc_type"], "text": "", "pages": [], "units": []})
        s = len(d["text"])
        d["text"] += render_text(r) + "\n\n"
        d["pages"].append((s, len(d["text"]), r["page"]))
        d["units"].append((s, len(d["text"]), page_sid(r["file"], r["page"])))
    return docs


def overlaps(spans, s, e, min_chars=30):
    """[s, e) 구간과 min_chars 이상(짧은 단위는 절반 이상) 겹치는 span 의 값들."""
    out = []
    for a, b, v in spans:
        ov = min(b, e) - max(a, s)
        if ov > 0 and ov >= min(min_chars, (b - a) * 0.5):
            out.append(v)
    return out


# ------------------------------------------------------------------ 전략 1: recursive
def chunk_recursive(size, overlap, add_title):
    name = f"recursive_{size}_{overlap}" + ("_title" if add_title else "") + NAME_SUFFIX
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=size, chunk_overlap=overlap, add_start_index=True,
        separators=["\n\n", "\n", ". ", "다. ", " ", ""])
    chunks = []
    for file, d in build_documents().items():
        for i, doc in enumerate(splitter.create_documents([d["text"]])):
            s = doc.metadata["start_index"]
            e = s + len(doc.page_content)
            pages = overlaps(d["pages"], s, e, min_chars=1)
            sids = overlaps(d["units"], s, e)
            chunks.append(make_chunk(name, file, d["doc_type"], pages, sids, "", doc.page_content,
                                     f"{name}__{slug(file)}__{i:04d}", add_title))
    return name, chunks


# ------------------------------------------------------------------ 전략 2: structural
def split_long(text, max_chars, first_sep=ARTICLE_PARA):
    """긴 텍스트를 항(①②…) 경계로 나누고, 그래도 길면 recursive 로 자른다."""
    if len(text) <= max_chars:
        return [text]
    parts = [p for p in first_sep.split(text) if p.strip()]
    packed, cur = [], ""
    for p in parts:
        if cur and len(cur) + len(p) > max_chars:
            packed.append(cur)
            cur = ""
        cur += p
    if cur:
        packed.append(cur)
    rs = RecursiveCharacterTextSplitter(chunk_size=max_chars, chunk_overlap=0, separators=["\n", ". ", " ", ""])
    return [q for p in packed for q in (rs.split_text(p) if len(p) > max_chars else [p])]


def sections_of(text):
    """제목·글머리표 줄을 경계로 단락 묶음을 만든다. 각 단락의 첫 줄을 단락 제목으로 사용."""
    secs, cur = [], []
    for line in text.split("\n"):
        if cur and HEADING.match(line.strip()):
            secs.append(cur)
            cur = []
        cur.append(line)
    if cur:
        secs.append(cur)
    return ["\n".join(s) for s in secs if "".join(s).strip()]


def figure_text(f):
    """그림 청크 본문. 우선순위: Gemini Markdown > OCR 텍스트(raster) > PDF 텍스트 레이어(벡터 차트)."""
    head = f"[그림] {f['caption']}".strip()
    if f.get("md") and not f["md"].startswith("유형: 장식"):
        warn = ""
        if f.get("unverified_numbers"):
            # 원문에 숫자로 적혀 있지 않은 값 = 그래프를 눈대중으로 읽은 추정값일 수 있음 → 답변 LLM 이 사실로 인용하지 않도록 표시
            warn = "\n※ 원문에 숫자로 적혀 있지 않은 그래프 판독값(인용 주의): " + ", ".join(f["unverified_numbers"][:20])
        return f"{head}\n{f['md']}{warn}", f["md_source"]
    if f["kind"] == "raster" and f.get("ocr_text"):
        return f"{head}\n{f['ocr_text']}", "ocr"
    if f["kind"] in ("vector_chart", "diagram"):
        # 글자를 도형으로 바꾼 차트·화살표 흐름도는 텍스트 레이어에 없는 글자가 많음 → OCR 결과를 함께
        parts = [f.get("text_layer") or ""]
        if f.get("ocr_text") and (f.get("hidden_chars") or 0) >= 10:
            parts.append(f"[OCR] {f['ocr_text']}")
        body = "\n".join(p for p in parts if p.strip())
        if body:
            return f"{head}\n{body}", "caption+text_layer" + ("+ocr" if len(parts) > 1 else "")
    return None, None   # lossy_table 은 내용이 이미 본문에 있고, 읽을 텍스트가 없는 그림은 VLM 변환 전까지 보류


def covered_by_figure(table, figs, ratio=0.6):
    """이 표(pdfplumber 추출)를 Gemini 가 변환한 그림 영역이 덮고 있는가.
    덮고 있으면 같은 내용이 두 번 들어가고, pdfplumber 쪽이 열 누락·행 쪼개짐으로 품질이 낮은 경우가 많아
    (주택연금 p.72: 업무내용 열 누락) Gemini 결과 하나만 남긴다."""
    tb = table.get("bbox")
    if not tb:
        return False
    t_area = max(1e-6, (tb[2] - tb[0]) * (tb[3] - tb[1]))
    for f in figs:
        if f.get("decorative") or not f.get("md") or f["md"].startswith("유형: 장식"):
            continue
        fb = f["bbox"]
        inter = max(0, min(tb[2], fb[2]) - max(tb[0], fb[0])) * max(0, min(tb[3], fb[3]) - max(tb[1], fb[1]))
        if inter >= ratio * t_area:
            return True
    return False


def chunk_structural(max_chars, add_title, with_figures=False):
    name = f"structural_{max_chars}" + ("_fig" if with_figures else "") + ("_title" if add_title else "") + NAME_SUFFIX
    chunks = []
    for u in load_jsonl(P / "units.jsonl"):
        if u["doc_type"] == "glossary":
            dic = u["source_id"].rsplit("-", 1)[1]
            chunks.append(make_chunk(name, u["file"], "glossary", u["pages"], [u["source_id"]], u["title"],
                                     u["text"], f"{name}__glossary__{dic}", add_title))
        elif u["doc_type"] == "terms":
            art_title = u["title"].split(" ", 1)[1] if " " in u["title"] else u["title"]
            art = u["source_id"].rsplit("-", 1)[1]
            for i, part in enumerate(split_long(u["text"], max_chars)):
                # 분할된 뒤쪽 조각에도 조항 제목을 본문 앞에 남겨 '어느 조항인지'를 잃지 않게 함
                text = part if i == 0 else f"{art_title} (계속)\n{part}"
                chunks.append(make_chunk(name, u["file"], "terms", u["pages"], [u["source_id"]], art_title,
                                         text, f"{name}__{slug(u['file'])}__a{art}__{i}", add_title, chunk_index=i))

    figs_by_region = {}
    if with_figures:
        for f in load_jsonl(ROOT / "data" / "figures" / "figures.jsonl"):
            if not f["decorative"]:
                figs_by_region.setdefault((f["file"], f["page"], f["half"]), []).append(f)

    for r in load_jsonl(P / PAGES_FILE):
        if r["doc_type"] not in ("life_guide", "additional"):
            continue
        sid = page_sid(r["file"], r["page"])
        tag = f"p{r['page']}{r['half'] or ''}"
        base = dict(half=r["half"], printed_page=r["printed_page"])
        items = []   # (content_type, title, text, meta)

        # ① 본문 텍스트: 제목·글머리표 경계로 나눠 max_chars 까지 묶음
        groups, cur = [], ""
        for sec in sections_of(r["text"]):
            if cur and len(cur) + len(sec) > max_chars:
                groups.append(cur)
                cur = ""
            cur += ("\n" if cur else "") + sec
        if cur:
            groups.append(cur)
        for g in (q for grp in groups for q in split_long(grp, max_chars, re.compile(r"(?=\n)"))):
            items.append(("text", g.strip().split("\n", 1)[0][:40], g, {}))

        # ② 표: 깨끗하게 추출된 표만 (lossy 표는 본문에 원문이 있고, 그림 단계에서 VLM 으로 다시 읽음)
        region_figs = figs_by_region.get((r["file"], r["page"], r["half"]), []) if with_figures else []
        for t in r["tables"]:
            if not t["lossy"] and not covered_by_figure(t, region_figs):
                items.append(("table", t["caption"][:40] or "표", f"[표] {t['caption']}\n{t['markdown']}",
                              {"source": "pdf_table", "caption": t["caption"]}))

        # ③ 그림: --figures 면 figures.jsonl 의 그림별 청크, 아니면 페이지의 숫자 줄 묶음(기존 방식)
        if with_figures:
            for f in figs_by_region.get((r["file"], r["page"], r["half"]), []):
                text, src = figure_text(f)
                if text:
                    items.append(("figure", f["caption"][:40] or "그림", text, {
                        "figure_kind": f["kind"], "source": src, "bbox": f["bbox"],
                        "image_path": f["image_path"], "caption": f["caption"], "num_check": f["num_check"]}))
        elif r["figure_lines"]:
            items.append(("figure", "그림·표 수치", "[그림·표 수치] " + " / ".join(r["figure_lines"]),
                          {"source": "text_layer"}))

        for i, (ctype, title, text, meta) in enumerate(items):
            chunks.append(make_chunk(name, r["file"], r["doc_type"], [r["page"]], [sid], title, text,
                                     f"{name}__{slug(r['file'])}__{tag}__{i}", add_title,
                                     **base, chunk_index=i, content_type=ctype, **meta))
    return name, chunks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", choices=["recursive", "structural", "parentchild"], required=True)
    ap.add_argument("--child-max", type=int, default=400, help="parentchild: child 최대 글자 수")
    ap.add_argument("--size", type=int, default=500)
    ap.add_argument("--overlap", type=int, default=50)
    ap.add_argument("--max-chars", type=int, default=1200)
    ap.add_argument("--title", action="store_true", help="embed_text 에 [문서명 > 제목] 접두어")
    ap.add_argument("--figures", action="store_true", help="data/figures/figures.jsonl 의 그림별 청크 사용")
    ap.add_argument("--pages", default="pages_v2.jsonl",
                    help="가이드북·추가 PDF 입력 (data/processed 안). 기본값이 아니면 청크 이름에 _로더명 이 붙음")
    a = ap.parse_args()
    global PAGES_FILE, NAME_SUFFIX
    PAGES_FILE = a.pages
    if a.pages != "pages_v2.jsonl":   # pages_p4l_layout.jsonl → _p4l_layout
        NAME_SUFFIX = "_" + a.pages.removeprefix("pages_").removesuffix(".jsonl")
    # python -m 실행 시 이 파일은 __main__ 이라, parent_child 가 import 하는 src.chunking 에도 같은 값을 넣어 둔다
    from src import chunking as mod
    mod.PAGES_FILE, mod.NAME_SUFFIX = PAGES_FILE, NAME_SUFFIX
    if a.strategy == "parentchild":
        from src import parent_child
        return parent_child.save(*parent_child.build(a.child_max, a.title))
    if a.strategy == "recursive":
        name, chunks = chunk_recursive(a.size, a.overlap, a.title)
    else:
        name, chunks = chunk_structural(a.max_chars, a.title, a.figures)
    OUT.mkdir(parents=True, exist_ok=True)
    ids = [c["chunk_id"] for c in chunks]
    assert len(ids) == len(set(ids)), "chunk_id 중복"
    with open(OUT / f"{name}.jsonl", "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"{name}: {len(chunks)} chunks -> {OUT / (name + '.jsonl')}")


if __name__ == "__main__":
    main()
