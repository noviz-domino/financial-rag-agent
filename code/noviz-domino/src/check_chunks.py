"""청킹 전략 비교 (LLM·임베딩 없이 비용 0).

각 청크 파일에 대해
  1) 통계      : 청크 수, 길이 중간값/p90/최대, 청크당 source_id 수(높을수록 여러 근거가 섞임)
  2) 근거 커버리지: 정답 source_id 중 어떤 청크에도 연결되지 않은 것의 수 (0이어야 평가가 성립)
  3) BM25 검색 : 세트별 검색 범위를 지켜서 Hit@1/3/5, Recall@5, MRR
       positive(POS001~060) → 용어집+약관 / life_guide(POS061~080) → 가이드북 / additional(ADD-*) → 추가 PDF

임베딩 검색 비교는 API 키를 정한 뒤 retrieval 단계에서 같은 지표로 추가하면 된다.
  4) 모드: chunk(청크 순위) / parent(parent 단위로 접기, parent-child 전략만) / filter(메타데이터 필터 hard)
     + ctx_chars@5 : top-5 로 LLM 에 들어갈 글자 수 (토큰 비용 대리 지표)

실행:  python -m src.check_chunks                 # data/chunks/*.jsonl 전부
       python -m src.check_chunks recursive_500_50 structural_1200
"""
import functools
import json
import statistics
import sys
from pathlib import Path

from kiwipiepy import Kiwi
from rank_bm25 import BM25Okapi

ROOT = Path(__file__).resolve().parent.parent
CH = ROOT / "data" / "chunks"
kiwi = Kiwi()
SETS = {
    "positive": ("positive_cases.jsonl", {"glossary", "terms"}),
    "life_guide": ("positive_life_guide_cases.jsonl", {"life_guide"}),
    "additional": ("additional_cases.jsonl", {"additional"}),
}


@functools.lru_cache(maxsize=None)
def tok(text):
    # 같은 청크를 모드(chunk/parent/filter)·전략마다 다시 분석하지 않도록 캐시 (형태소 분석이 실행 시간의 대부분)
    return tuple(t.form for t in kiwi.tokenize(text) if t.tag[0] in "NVX" or t.tag in ("SL", "SN", "SH"))


def load_jsonl(p):
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def evaluate(name, k_list=(1, 3, 5), mode="chunk"):
    """mode
      chunk  : 청크(=child) 순위 그대로
      parent : child 순위를 parent 단위로 접어서 top-k (small-to-big: LLM 에는 parent 가 들어감)
      filter : src.metadata_filter.suggest_filter(hard=True) 로 후보를 제한한 뒤 child 순위
    ctx_chars@5 : top-5 로 LLM 에 넘어갈 글자 수 평균 (parent 모드는 parent 본문 길이) → 토큰 비용 대리 지표
    """
    from src.metadata_filter import suggest_filter
    chunks = load_jsonl(CH / f"{name}.jsonl")
    parents = {}
    if mode == "parent":
        pfile = CH / "parents" / f"{name}.jsonl"
        if not pfile.exists():
            return None
        parents = {p["chunk_id"]: p for p in load_jsonl(pfile)}
    lens = sorted(c["n_chars"] for c in chunks)
    row = {
        "strategy": name + ("" if mode == "chunk" else f" [{mode}]"), "n_chunks": len(chunks),
        "len_p50": int(statistics.median(lens)), "len_p90": lens[int(len(lens) * 0.9)], "len_max": lens[-1],
        "sids_per_chunk": round(statistics.mean(len(c["source_ids"]) for c in chunks), 2),
    }
    covered = {s for c in chunks for s in c["source_ids"]}
    missing, ctx = 0, []
    scope_name = {"positive": "base", "life_guide": "life_guide", "additional": "additional"}
    for set_name, (fname, scope) in SETS.items():
        cases = load_jsonl(ROOT / "03_answers" / fname)
        missing += sum(1 for c in cases for s in c["expected_source_ids"] if s not in covered)
        pool = [c for c in chunks if c["doc_type"] in scope]
        bm25 = BM25Okapi([tok(c["embed_text"]) for c in pool])
        hits = {k: 0 for k in k_list}
        recall, mrr = 0.0, 0.0
        for case in cases:
            gold = set(case["expected_source_ids"])
            scores = bm25.get_scores(tok(case["question"]))
            order = sorted(range(len(pool)), key=lambda i: -scores[i])
            if mode == "filter":
                flt = suggest_filter(case["question"], scope_name[set_name], hard=True)
                order = [i for i in order if flt.match(pool[i])] or order   # 필터 결과가 비면 필터 해제
            ranked = []
            if mode == "parent":   # child 순위 → 처음 등장하는 parent 순서
                seen = set()
                for i in order:
                    pid = pool[i].get("parent_id")
                    if pid and pid not in seen:
                        seen.add(pid)
                        ranked.append(parents[pid])
                    if len(ranked) >= max(k_list):
                        break
            else:
                ranked = [pool[i] for i in order[:max(k_list)]]
            for k in k_list:
                hits[k] += bool(gold & {s for c in ranked[:k] for s in c["source_ids"]})
            recall += len(gold & {s for c in ranked[:5] for s in c["source_ids"]}) / len(gold)
            mrr += next((1 / (r + 1) for r, c in enumerate(ranked) if gold & set(c["source_ids"])), 0)
            ctx.append(sum(c["n_chars"] for c in ranked[:5]))
        n = len(cases)
        for k in k_list:
            row[f"{set_name}_hit@{k}"] = round(hits[k] / n, 3)
        row[f"{set_name}_recall@5"] = round(recall / n, 3)
        row[f"{set_name}_mrr"] = round(mrr / n, 3)
    row["ctx_chars@5"] = int(statistics.mean(ctx))
    row["gold_missing"] = missing
    return row


def main():
    names = sys.argv[1:] or sorted(p.stem for p in CH.glob("*.jsonl"))
    rows = []
    for n in names:
        rows.append(evaluate(n))
        if (CH / "parents" / f"{n}.jsonl").exists():      # parent-child 전략이면 parent 모드도 함께
            rows.append(evaluate(n, mode="parent"))
        rows.append(evaluate(n, mode="filter"))
    cols = list(rows[0])
    print("| " + " | ".join(cols) + " |")
    print("|" + "---|" * len(cols))
    for r in rows:
        print("| " + " | ".join(str(r[c]) for c in cols) + " |")
    out = CH / "compare_bm25.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n-> {out}")


if __name__ == "__main__":
    main()
