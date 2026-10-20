"""준비 파이프라인: PDF → 쪽 레코드 → 정답 근거 단위 → 그림 → 청크 → (임베딩·Chroma) 를 명령 하나로.

  python -m src.pipeline.build --config configs/baseline.json            # 전체 (API 키 필요: 그림 설명·임베딩)
  python -m src.pipeline.build --config configs/baseline.json --no-vlm   # 그림·표 Gemini 생략 (임베딩은 --no-index 로 생략)
  python -m src.pipeline.build --config ... --no-vlm --no-index          # API 없이 청크까지
  python -m src.pipeline.build --config ... --offline                    # 가짜 AI·가짜 임베딩 (키 없이 끝까지)
  --variants all  : structural / recursive_500_50 / parentchild_400 청크를 모두 만든다 (청킹 실험용)
  --reuse         : 이미 있는 pages/sources/figures 를 다시 읽지 않고 청킹부터
산출물(data/): pages.jsonl, sources.jsonl, figures.jsonl, figure_review.jsonl, chunks/{variant}.jsonl(+.parents.jsonl)
"""
from __future__ import annotations

import argparse
import copy
import json
import time
from collections import Counter
from pathlib import Path

from src.common.config import ROOT, chunk_variant_name, data_dir, load_config
from src.common.llm import make_client
from src.common.schemas import check_chunk, check_page

PDF_ROOT = ROOT / "01_data"
ALL_VARIANTS = [{"strategy": "structural"}, {"strategy": "recursive"}, {"strategy": "parentchild"}]


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_jsonl(path: Path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def pdf_list():
    return sorted(PDF_ROOT.rglob("*.pdf"))


def stage_pages(cfg, d: Path, log=print):
    from src.loading.loader import load_pdf
    all_recs, repeated = [], {}
    for path in pdf_list():
        t0 = time.time()
        recs, rep = load_pdf(path, cfg)
        repeated[path.name] = sorted(rep)
        for r in recs:
            r.pop("_bbox", None)
            check_page(r)
        log(f"  읽기 {path.name[:40]:40s} 레코드 {len(recs):4d} 표 {sum(len(r['tables']) for r in recs):3d} "
            f"({time.time() - t0:.1f}s)")
        all_recs += recs
    write_jsonl(d / "pages.jsonl", all_recs)
    (d / "pages_meta.json").write_text(json.dumps({"repeated": repeated}, ensure_ascii=False), encoding="utf-8")
    return all_recs, repeated


def stage_sources(d: Path, repeated, log=print):
    from src.sources.units import build_units
    units, warns = build_units(pdf_list(), {k: set(v) for k, v in repeated.items()})
    for w in warns:
        log("  [warn] " + w)
    write_jsonl(d / "sources.jsonl", units)
    return units


def stage_figures(cfg, d: Path, pages, client, use_vlm, log=print):
    from src.figures.ocr import make_ocr
    from src.figures.run import build_figures
    fc = cfg["figures"]
    if not fc.get("enabled", True):
        write_jsonl(d / "figures.jsonl", [])
        return []
    ocr, engine = make_ocr(fc["ocr_engine"], fc.get("ocr_scale", 2.0))
    log(f"  OCR 엔진: {engine} (설정 {fc['ocr_engine']}) | Gemini 그림·표 설명: {'켬' if use_vlm else '끔(--no-vlm)'}")
    by_file = {}
    for r in pages:
        by_file.setdefault(r["file"], []).append(r)
    figs = []
    for path in pdf_list():
        recs = by_file[path.name]
        if recs[0]["doc_type"] not in ("life_guide", "additional"):
            continue
        t0 = time.time()
        fs = build_figures(path, recs, cfg, ocr, client, d / "figures", use_vlm)
        kinds = Counter(f["kind"] for f in fs)
        log(f"  그림 {path.name[:40]:40s} 후보 {len(fs):3d} {dict(kinds)} 제외 {sum(f['decorative'] for f in fs)} "
            f"({time.time() - t0:.1f}s)")
        figs += fs
    rows = [{k: v for k, v in f.items() if not k.startswith("_")} for f in figs]
    write_jsonl(d / "figures.jsonl", rows)
    write_jsonl(d / "figure_review.jsonl", [{"figure_id": f["figure_id"], "file": f["file"], "page": f["page"],
                                              "reasons": f["review_reasons"], "image_path": f["image_path"],
                                              "description": f["description"][:300]}
                                             for f in rows if f.get("review_reasons")])
    return rows


def stage_chunks(cfg, d: Path, units, pages, figures, log=print):
    from src.chunking.build import make_chunks
    paths = {p.name: p for p in pdf_list()}
    variant, chunks, parents = make_chunks(cfg, units, pages, figures, paths)
    for c in chunks:
        check_chunk(c)
    write_jsonl(d / "chunks" / f"{variant}.jsonl", chunks)
    write_jsonl(d / "chunks" / f"{variant}.parents.jsonl", parents)
    return variant, chunks, parents


def ensure_chunks(cfg: dict, log=print):
    """평가 실행 때: 설정의 청킹 변형 파일이 없으면 pages/sources/figures 로 바로 만든다 (임베딩 없음)."""
    d = data_dir(cfg)
    variant = chunk_variant_name(cfg)
    if (d / "chunks" / f"{variant}.jsonl").exists():
        return variant
    for need in ("pages.jsonl", "sources.jsonl", "figures.jsonl"):
        if not (d / need).exists():
            raise FileNotFoundError(f"{d / need} 가 없어요. 먼저: python -m src.pipeline.build --config <설정> --no-vlm --no-index")
    log(f"청크 {variant} 가 없어 pages/sources/figures 에서 만들어요")
    stage_chunks(cfg, d, read_jsonl(d / "sources.jsonl"), read_jsonl(d / "pages.jsonl"), read_jsonl(d / "figures.jsonl"), log)
    return variant


def report(cfg, d: Path, pages, units, chunks_by_variant, log=print):
    from src.evaluation.dataset import load_all_answers
    from src.sources.check import check
    halves = sum(1 for r in pages if r["half"])
    log("\n=== 빌드 요약 ===")
    log(f"쪽(반쪽) 레코드 {len(pages)}개 (펼침면 반쪽 {halves}개) | 텍스트 레이어 없는 쪽 "
        f"{sum(1 for r in pages if not r['has_text_layer'])}개")
    for r in pages:
        if not r["has_text_layer"]:
            log(f"   텍스트 없음: {r['file']} p.{r['page']}{r['half'] or ''}")
    log(f"표 상태: {dict(Counter(t['status'] for r in pages for t in r['tables']))}")
    log(f"근거 단위 {len(units)}개 {dict(Counter(u['doc_type'] for u in units))}")
    answers = load_all_answers(cfg)
    for variant, chunks in chunks_by_variant.items():
        by = Counter(c["doc_type"] for c in chunks)
        ct = Counter(c["content_type"] for c in chunks)
        res = check(units, chunks, answers)
        log(f"[{variant}] 청크 {len(chunks)}개 doc_type별 {dict(by)} 내용유형별 {dict(ct)}")
        log(f"   정답 근거 누락 {len(res['missing_in_units']) + len(res['missing_in_chunks'])} "
            f"(근거 단위 {len(res['missing_in_units'])}, 청크 {len(res['missing_in_chunks'])}) / expected_source_ids {res['total']}개 "
            f"| 핵심어 불일치 문항 {len(res['keyword_miss'])}")
        for x in (res["missing_in_units"] + res["missing_in_chunks"])[:10]:
            log(f"      누락 {x}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="configs/baseline.json")
    ap.add_argument("--no-vlm", action="store_true", help="그림·표 Gemini 변환 생략")
    ap.add_argument("--no-index", action="store_true", help="임베딩·Chroma 색인 생략")
    ap.add_argument("--offline", action="store_true", help="가짜 AI·가짜 임베딩 사용 (API 키 불필요)")
    ap.add_argument("--variants", choices=["config", "all"], default="config")
    ap.add_argument("--reuse", action="store_true", help="기존 pages/sources/figures 재사용")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    d = data_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    need_ai = not a.no_index or not a.no_vlm
    client = make_client(cfg, offline=a.offline) if (need_ai or a.offline) else None

    if a.reuse and (d / "pages.jsonl").exists() and (d / "sources.jsonl").exists() and (d / "figures.jsonl").exists():
        pages, units, figures = (read_jsonl(d / f"{n}.jsonl") for n in ("pages", "sources", "figures"))
        print("기존 pages/sources/figures 재사용")
    else:
        print("[1/4] PDF 읽기")
        pages, repeated = stage_pages(cfg, d)
        print("[2/4] 정답 근거 ID 연결")
        units = stage_sources(d, repeated)
        print("[3/4] 그림")
        figures = stage_figures(cfg, d, pages, client, use_vlm=not a.no_vlm, log=print)
    print("[4/4] 청킹")
    variants = [cfg]
    if a.variants == "all":
        variants = []
        for v in ALL_VARIANTS:
            c = copy.deepcopy(cfg)
            c["chunking"]["strategy"] = v["strategy"]
            variants.append(c)
    chunks_by_variant = {}
    for c in variants:
        name, chunks, _ = stage_chunks(c, d, units, pages, figures)
        chunks_by_variant[name] = chunks
        if not a.no_index:
            from src.index.store import build_index
            build_index(c, chunks, client, log=print)
    report(cfg, d, pages, units, chunks_by_variant)
    print(f"\n완료 ({time.time() - t0:.0f}s) -> {d}")


if __name__ == "__main__":
    main()
