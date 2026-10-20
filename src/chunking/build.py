"""청킹 전략 선택: structural(기준) · recursive_{size}_{overlap} · parentchild_{child_max}.

  structural    용어·조항·제목 단위 (structural.py)
  recursive     글자 수로 자르기. 용어집·약관은 근거 단위 본문을 이어 붙인 글, 가이드북·추가는 쪽(반쪽) 본문+표+그림 글
  parentchild   구조형을 child_max(400자)로 더 잘게 자른 child + 조항 전체·반쪽 페이지 parent (검색에서 parent 로 접어 쓰는 실험)
"""
from __future__ import annotations

from collections import defaultdict

from src.chunking.common import make_chunk, overlaps, page_source_id, recursive_split, slug
from src.chunking.links import add_links
from src.chunking.structural import (chunk_glossary, chunk_guide_file, chunk_terms, figure_chunk_text)
from src.common.config import chunk_variant_name


def _recursive(units, pages_by_file, figs_by_file, variant, size, overlap):
    chunks = []
    unit_by_file = defaultdict(list)
    for u in units:
        if u["doc_type"] in ("glossary", "terms"):
            unit_by_file[u["file"]].append(u)
    for file, us in unit_by_file.items():
        text, uspans, pspans = "", [], []
        for u in us:
            s = len(text)
            text += u["text"] + "\n\n"
            uspans.append((s, len(text), u["source_id"]))
            pspans += [(s, len(text), p) for p in u["pages"]]
        for i, (piece, start) in enumerate(recursive_split(text, size, overlap)):
            e = start + len(piece)
            sids = overlaps(uspans, start, e)
            pages = sorted(set(overlaps(pspans, start, e, min_chars=1)))
            chunks.append(make_chunk(f"{variant}__{slug(file)}__{i:04d}", file, us[0]["doc_type"], pages, "", piece,
                                     source_ids=sids, chunk_index=i, strategy=variant))
    for file, recs in pages_by_file.items():
        if recs[0]["doc_type"] not in ("life_guide", "additional"):
            continue
        figs = defaultdict(list)
        for f in figs_by_file.get(file, []):
            figs[(f["page"], f["half"])].append(f)
        text, pspans, spans = "", [], []
        for r in recs:
            parts = [r["text"]] + [f"[표] {t['caption']}\n{t['markdown']}" for t in r["tables"] if not t["lossy"]]
            for f in figs.get((r["page"], r["half"]), []):
                if not f.get("decorative"):
                    t, _, _ = figure_chunk_text(f)
                    if t:
                        parts.append(t)
            s = len(text)
            text += "\n".join(p for p in parts if p) + "\n\n"
            pspans.append((s, len(text), r["page"]))
        for i, (piece, start) in enumerate(recursive_split(text, size, overlap)):
            pages = sorted(set(overlaps(pspans, start, start + len(piece), min_chars=1)))
            chunks.append(make_chunk(f"{variant}__{slug(file)}__{i:04d}", file, recs[0]["doc_type"], pages, "", piece,
                                     source_ids=[page_source_id(file, p) for p in pages], chunk_index=i,
                                     strategy=variant))
        covered = {p for c in chunks if c["file"] == file for p in c["pages"]}
        for r in recs:      # 어떤 청크에도 덮이지 않은 쪽(본문 없음)에도 보조 청크 → 정답 근거 누락 0
            if r["page"] not in covered:
                chunks.append(make_chunk(f"{variant}__{slug(file)}__p{r['page']}fb", file, r["doc_type"], [r["page"]], "",
                                         r["text"] or f"[{r['page']}쪽: 추출 가능한 텍스트 없음]",
                                         source_ids=[page_source_id(file, r["page"])], strategy=variant, fallback=True))
                covered.add(r["page"])
    return chunks, []


def make_chunks(cfg, units, pages, figures, pdf_paths):
    """반환: (variant 이름, chunks, parents).
    units: sources.jsonl 행, pages: pages.jsonl 행, figures: figures.jsonl 행, pdf_paths: {파일명: 경로}."""
    c = cfg["chunking"]
    strategy, variant = c["strategy"], chunk_variant_name(cfg)
    pages_by_file = defaultdict(list)
    for r in pages:
        pages_by_file[r["file"]].append(r)
    figs_by_file = defaultdict(list)
    for f in figures:
        figs_by_file[f["file"]].append(f)
    if strategy == "recursive":
        chunks, parents = _recursive(units, pages_by_file, figs_by_file, variant, c["size"], c["overlap"])
    else:
        max_chars = c["max_chars"] if strategy == "structural" else c["child_max"]
        chunks, parents = [], []
        g = [u for u in units if u["doc_type"] == "glossary"]
        t = [u for u in units if u["doc_type"] == "terms"]
        a, p = chunk_glossary(g, variant)
        chunks += a
        parents += p
        a, p = chunk_terms(t, variant, max_chars)
        chunks += a
        parents += p
        for file, recs in pages_by_file.items():
            if recs[0]["doc_type"] in ("life_guide", "additional"):
                a, p = chunk_guide_file(file, recs[0]["doc_type"], recs, pdf_paths[file], figs_by_file.get(file, []),
                                        variant, max_chars, c.get("short_min", 100))
                chunks += a
                parents += p
    ids = [x["chunk_id"] for x in chunks]
    assert len(ids) == len(set(ids)), "chunk_id 중복"
    add_links(chunks)
    return variant, chunks, parents
