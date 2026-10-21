"""청킹까지의 전체 결과를 한 번에 검증하고 docs/검증_결과.md 로 남긴다.

검증 항목
  1. 정답 근거 연결 : 정답 source_id 251개가 모두 근거 단위에 있는가 + 기준 답안 핵심어가 연결 원문에 있는가
  2. 청크 파일      : 전략별 청크 수, 내용 종류(text/table/figure), 정답 근거 누락
  3. 그림 변환      : 변환 완료·오류·장식 수, 숫자 검증 분포, '원문 미표기 숫자' 경고가 붙은 청크 수
  4. 링크           : 그림·표 참조가 정말 같은 라벨의 그림을 가리키는가, 조항 참조 수
  5. 중복 제거      : Gemini 그림이 덮어서 빠진 pdfplumber 표 수
  6. 검색 비교      : src.check_chunks 결과 (BM25)

실행:  python -m src.verify_all            (BM25 비교까지 약 5분)
       python -m src.verify_all --quick    (BM25 비교 생략)
"""
import argparse
import json
import re
from collections import Counter
from pathlib import Path

from kiwipiepy import Kiwi

ROOT = Path(__file__).resolve().parent.parent
P = ROOT / "data" / "processed"
CH = ROOT / "data" / "chunks"
FIG = ROOT / "data" / "figures"


def load_jsonl(p):
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def check_gold(lines):
    kiwi = Kiwi()
    units = {u["source_id"]: u for u in load_jsonl(P / "units.jsonl")}
    lines.append("## 1. 정답 근거 연결")
    lines.append("| 세트 | 문항 | 근거 ID | 누락 | 핵심어 일치율 평균 | 0.5 미만 문항 |")
    lines.append("|---|---|---|---|---|---|")

    def keys(t):
        return {x.form for x in kiwi.tokenize(t) if x.tag in ("NNG", "NNP") and len(x.form) >= 2} | set(re.findall(r"\d+", t))

    total_missing = 0
    low_cases = []
    for name, fname in (("기본 positive", "positive_cases.jsonl"), ("가이드북", "positive_life_guide_cases.jsonl"),
                        ("추가 PDF", "additional_cases.jsonl")):
        cases = load_jsonl(ROOT / "03_answers" / fname)
        ids = [s for c in cases for s in c["expected_source_ids"]]
        missing = [s for s in ids if s not in units]
        total_missing += len(missing)
        scores = []
        for c in cases:
            src = " ".join(units[s]["text"] for s in c["expected_source_ids"] if s in units)
            ks = keys(c["reference_answer"])
            sc = sum(1 for k in ks if k in src) / max(len(ks), 1)
            scores.append(sc)
            if sc < 0.5:
                low_cases.append((c["case_id"], round(sc, 2)))
        lines.append(f"| {name} | {len(cases)} | {len(ids)} | {len(missing)} | {sum(scores) / len(scores):.2f} | "
                     f"{sum(1 for s in scores if s < 0.5)} |")
    lines.append(f"\n- 핵심어 일치율 0.5 미만 문항: {low_cases} (기준 답안이 원문을 바꿔 쓴 경우가 많음. 연결 오류 의심 시 직접 확인)")
    lines.append("- 가이드북·추가 PDF 근거는 페이지 단위라 원문이 길어 일치율이 높게 나오는 편\n")
    return total_missing


def check_chunks_files(lines):
    lines.append("## 2. 청크 파일")
    lines.append("| 전략 | 청크 수 | text | table | figure | 정답 근거 누락 |")
    lines.append("|---|---|---|---|---|---|")
    gold = {s for f in ("positive_cases.jsonl", "positive_life_guide_cases.jsonl", "additional_cases.jsonl")
            for c in load_jsonl(ROOT / "03_answers" / f) for s in c["expected_source_ids"]}
    for p in sorted(CH.glob("*.jsonl")):
        cs = load_jsonl(p)
        ct = Counter(c.get("content_type", "text") for c in cs)
        covered = {s for c in cs for s in c["source_ids"]}
        lines.append(f"| {p.stem} | {len(cs)} | {ct['text']} | {ct['table']} | {ct['figure']} | {len(gold - covered)} |")
    lines.append("")


def check_figures(lines):
    figs = load_jsonl(FIG / "figures.jsonl")
    t = [f for f in figs if not f["decorative"] and not f["dup_of"]]
    done = [f for f in t if f.get("md")]
    nc = [f["num_check"] for f in done if f.get("num_check") is not None]
    bins = Counter("0.9 이상" if x >= .9 else "0.7~0.9" if x >= .7 else "0.5~0.7" if x >= .5 else "0.5 미만" for x in nc)
    lines.append("## 3. 그림 변환")
    lines.append(f"- 그림 후보 {len(figs)}개 → 변환 대상 {len(t)}개 → 변환 완료 {len(done)}개, "
                 f"오류 {sum(1 for f in t if f.get('vlm_error'))}개, Gemini 가 장식으로 판정 {sum(1 for f in done if f['md'].startswith('유형: 장식'))}개")
    lines.append(f"- 숫자 검증 분포: {dict(bins)} (숫자 없는 그림 {len(done) - len(nc)}개)")
    lines.append(f"- '원문에 숫자로 적혀 있지 않은 그래프 판독값' 경고 대상: {sum(1 for f in done if f.get('unverified_numbers'))}개")
    kinds = {}
    for k in ("lossy_table", "vector_chart", "raster", "diagram"):
        xs = [f["num_check"] for f in done if f["kind"] == k and f.get("num_check") is not None]
        kinds[k] = round(sum(xs) / len(xs), 2) if xs else None
    lines.append(f"- 종류별 숫자 검증 평균: {kinds}\n")


def check_links(lines, name="parentchild_400"):
    p = CH / f"{name}.jsonl"
    if not p.exists():
        return
    ch = {c["chunk_id"]: c for c in load_jsonl(p)}
    pa = load_jsonl(CH / "parents" / f"{name}.jsonl")
    roman = {"I": "Ⅰ", "II": "Ⅱ", "III": "Ⅲ", "IV": "Ⅳ", "V": "Ⅴ"}

    def variants(label):
        m = re.match(r"(그림|참고|표)(.*)", label)
        kind, num = m.group(1), m.group(2)
        out = {kind + num}
        for k, v in sorted(roman.items(), key=lambda kv: -len(kv[0])):
            if num.startswith(k):
                out.add(kind + v + num[len(k):])
        return out

    seen, ok, bad = set(), 0, []
    for c in ch.values():
        for r in c.get("refs", []):
            if r["type"] != "figure" or (r["label"], r["target"]) in seen:
                continue
            seen.add((r["label"], r["target"]))
            t = ch[r["target"]]
            hay = re.sub(r"\s+", "", (t.get("caption") or "") + t["text"][:150]).replace(".", "-")
            if any(v in hay for v in variants(r["label"])):
                ok += 1
            else:
                bad.append(r["label"])
    arts = sum(1 for p_ in pa for r in p_.get("refs", []) if r["type"] == "article")
    lines.append(f"## 4. 링크 ({name})")
    lines.append(f"- 그림·표 참조: {ok + len(bad)}개 중 대상 캡션에 같은 라벨이 있는 것 {ok}개, 의심 {len(bad)}개 {bad[:10]}")
    lines.append(f"- 조항 참조(같은 약관 안): {arts}개\n")


def check_dedup(lines):
    from src.chunking import covered_by_figure
    figs = {}
    for f in load_jsonl(FIG / "figures.jsonl"):
        if not f["decorative"]:
            figs.setdefault((f["file"], f["page"], f["half"]), []).append(f)
    dropped = []
    for r in load_jsonl(P / "pages_v2.jsonl"):
        for t in r["tables"]:
            if not t["lossy"] and covered_by_figure(t, figs.get((r["file"], r["page"], r["half"]), [])):
                dropped.append(f"{r['file'][:20]} p{r['page']}{r['half'] or ''}")
    lines.append("## 5. 중복 제거")
    lines.append(f"- Gemini 그림이 덮어서 청크에서 뺀 pdfplumber 표: {len(dropped)}개 (예: {dropped[:6]})\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    lines = ["# 청킹까지 전체 검증 결과", "", "`python -m src.verify_all` 로 다시 만들 수 있어요.", ""]
    missing = check_gold(lines)
    check_chunks_files(lines)
    check_figures(lines)
    check_links(lines)
    check_dedup(lines)
    if not a.quick:
        import sys
        from src import check_chunks
        names = [p.stem for p in sorted(CH.glob("*.jsonl"))]
        sys.argv = ["check_chunks"] + names
        check_chunks.main()
        rows = json.load(open(CH / "compare_bm25.json", encoding="utf-8"))
        cols = ["strategy", "positive_hit@1", "positive_hit@5", "positive_mrr", "life_guide_hit@1", "life_guide_hit@5",
                "additional_hit@1", "additional_hit@5", "additional_mrr", "ctx_chars@5"]
        heads = ["전략", "기본 H@1", "기본 H@5", "기본 MRR", "가이드 H@1", "가이드 H@5", "추가 H@1", "추가 H@5", "추가 MRR",
                 "LLM 입력 글자(top5)"]
        lines.append("## 6. 검색 비교 (BM25 + Kiwi, 세트별 검색 범위 준수)")
        lines.append("| " + " | ".join(heads) + " |")
        lines.append("|" + "---|" * len(heads))
        for r in rows:
            lines.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
        lines.append("\n문항 수: 기본 60 / 가이드 20(1문항 = 0.05) / 추가 60")
    out = ROOT / "docs" / "검증_결과.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n-> {out}")
    if missing:
        raise SystemExit(f"정답 근거 누락 {missing}개")


if __name__ == "__main__":
    main()
