"""평가 루프: 질문 실행 → 검색 근거·답변·시간 저장 → 채점 → 실패 분류 → runs/{exp_id}/ 기록.

  python -m src.evaluation.run --config configs/baseline.json --sets positive,negative,life_guide,additional
  python -m src.evaluation.run --config configs/baseline.json --sets positive --dry-run --limit 5      # 가짜 AI (키 불필요)
  python -m src.evaluation.run --config configs/exp2_bm25.json --sets positive --retrieval-only        # 검색 지표만 (BM25 면 API 불필요)

runs/{exp_id}/config.json  실제 쓴 설정 + 기준 대비 diff + 모델·프롬프트·버전
            results.jsonl  문항별 실행 결과 (답변·검색 청크·인용·시간·호출·토큰·trace)
            scores.jsonl   문항별 점수·실패 분류
            summary.json   전체·세트별 지표, 비용
runs/experiments.jsonl     실험 목록 (한 줄 요약)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from importlib import metadata
from pathlib import Path

from src.common.config import ROOT, BehaviorMap, data_dir, diff_config, load_config
from src.common.llm import make_client
from src.common.schemas import RunResult
from src.evaluation import metrics as M
from src.evaluation.dataset import load_set, read_jsonl
from src.evaluation.judge import score_answer
from src.generation import prompts as P


def run_one(case, retriever, client, cfg):
    if cfg["pipeline"] == "agent":
        from src.agent.graph import run_agent
        return run_agent(case, retriever, client, cfg)
    from src.generation.rag import run_rag
    return run_rag(case, retriever, client, cfg)


def retrieval_only(case, retriever, cfg):
    from src.common.llm import use_meter
    with use_meter() as m:
        res = retriever.search(case["question"], cfg["retrieval"]["k"], scope=case["scope"])
        return {"answer": "", "behavior": "", "citations": [], "injection": False, "clean_query": "",
                "retrieved": res, "queries": [case["question"]], "trace": [], "usage": m.finish()}


def score_case(case, out, bm: BehaviorMap, id2pages, client, cfg, with_answer=True):
    """문항 하나 채점. with_answer=False 면 검색 지표만."""
    ev = cfg["evaluation"]
    expected_ids = case.get("expected_source_ids", [])
    exp_pages = M.expected_pages(expected_ids, id2pages, case.get("expected_pages", []))
    retrieved = out["retrieved"]
    row = {"case_id": case["case_id"], "set": case["set"], "category": case.get("category"),
           "expected_behavior": case["expected_behavior"], "usage": out["usage"],
           "retrieval": M.retrieval_case_metrics(retrieved, expected_ids, exp_pages, cfg["retrieval"]["k"])}
    if not with_answer:
        return row
    e7 = bm.to7(case["expected_behavior"])
    p7 = out["behavior"] or "abstain"
    row.update(expected7=e7, expected5=bm.to5(e7), pred7=p7, pred5=bm.to5(p7))
    row.update(M.behavior_match(e7, p7, bm.to5))
    # 인용
    cites = M.parse_citations(out["answer"], out["citations"])
    cm = M.citation_metrics(cites, retrieved, exp_pages)
    row["citation"] = cm if (cites or (exp_pages and row["expected5"] in ("answer", "correct"))) else None
    # 필수·금지 내용: 20자 미만 = 규칙, 긴 문장·negative = AI
    neg = case["set"] == "negative"
    short = ev.get("short_item_chars", 20)
    r_inc, a_inc = M.split_items(case.get("must_include", []), neg, short)
    r_not, a_not = M.split_items(case.get("must_not_include", []), neg, short)
    sc = score_answer(client, cfg, case, out["answer"], retrieved, a_inc, a_not) if out["answer"] or out["behavior"] else \
        {"score": 0, "include": {}, "violated": {}, "hallucination": False, "reason": "빈 답변", "usage": {}}
    inc = {**M.rule_contains(out["answer"], r_inc), **sc["include"]}
    notv = {**M.rule_contains(out["answer"], r_not), **{k: v for k, v in sc["violated"].items()}}
    row.update(score=sc["score"], score_reason=sc["reason"], judge_usage=sc["usage"],
               must_include_detail=inc, must_include_rate=(sum(inc.values()) / len(inc)) if inc else None,
               must_not_detail=notv, must_not_violation_rate=(sum(notv.values()) / len(notv)) if notv else None,
               must_not_violated=any(notv.values()))
    ctx_text = " ".join(r["text"] for r in retrieved)
    row["unsupported_numbers"] = M.unsupported_numbers(out["answer"], ctx_text, case["question"])
    row["hallucination"] = sc["hallucination"]
    row["injection_expected"] = bm.is_injection(case["expected_behavior"])
    if row["injection_expected"]:
        row["injection_detected"] = bool(out.get("injection"))
        row["injection_handled"] = bool(row["match5"] and not row["must_not_violated"])
    hit5 = row["retrieval"]["id_hit5"] if row["retrieval"] else None
    row["failure"] = M.classify_failure(case, {"expected5": row["expected5"], "pred5": row["pred5"], "id_hit5": hit5,
                                               "score": row["score"], "hallucination": row["hallucination"],
                                               "expected7": e7, "pred7": p7,
                                               "must_not_violated": row["must_not_violated"]})
    return row


def _versions():
    out = {"python": platform.python_version()}
    for p in ("pdfplumber", "chromadb", "kiwipiepy", "rank-bm25", "google-genai", "langgraph"):
        try:
            out[p] = metadata.version(p)
        except metadata.PackageNotFoundError:
            out[p] = None
    return out


def _git_commit():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001
        return None


def write_config(out_dir: Path, exp_id, cfg, dry_run, sets, ret_only, chunk_stats):
    rec = {"exp_id": exp_id, "date": dt.datetime.now().isoformat(timespec="seconds"), "dry_run": dry_run,
           "retrieval_only": ret_only, "sets": sets, "config": cfg, "diff_vs_baseline": diff_config(cfg),
           "models": {"generation": cfg["llm"]["model"], "judge": cfg["llm"]["judge_model"],
                      "embedding": cfg["retrieval"]["embed_model"], "figure_vlm": cfg["figures"]["model"]},
           "prompts": P.all_prompts(), "prompt_version": P.prompt_version(), "chunks": chunk_stats,
           "versions": _versions(), "git_commit": _git_commit()}
    (out_dir / "config.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="configs/baseline.json")
    ap.add_argument("--sets", default="positive,negative,life_guide,additional",
                    help="positive,negative,life_guide,additional,golden 중 쉼표로")
    ap.add_argument("--dry-run", action="store_true", help="가짜 AI·가짜 임베딩 (API 키 불필요, 결과 폴더는 dry_ 접두어)")
    ap.add_argument("--retrieval-only", action="store_true", help="검색 지표만 (AI 호출 없음)")
    ap.add_argument("--pipeline", choices=["rag", "agent"], help="설정의 pipeline 덮어쓰기")
    ap.add_argument("--limit", type=int, help="세트마다 앞에서 N문항만")
    ap.add_argument("--case-ids", help="쉼표로 구분한 case_id 만")
    ap.add_argument("--exp-id", help="설정의 exp_id 덮어쓰기")
    ap.add_argument("--force", action="store_true", help="같은 exp_id 폴더가 있어도 덮어쓰기")
    ap.add_argument("--workers", type=int, default=1, help="동시 실행 수 (API 한도에 주의)")
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    if a.pipeline:
        cfg["pipeline"] = a.pipeline
    exp_id = a.exp_id or cfg["exp_id"]
    if a.dry_run and not exp_id.startswith("dry_"):
        exp_id = "dry_" + exp_id
    runs = Path(cfg["paths"]["runs"])
    runs = runs if runs.is_absolute() else ROOT / runs
    out_dir = runs / exp_id
    if out_dir.exists() and not a.force:
        sys.exit(f"{out_dir} 가 이미 있어요. 다른 --exp-id 를 쓰거나 --force 로 덮어쓰세요.")
    out_dir.mkdir(parents=True, exist_ok=True)

    bm = BehaviorMap.load()
    sets = [s.strip() for s in a.sets.split(",") if s.strip()]
    cases = []
    for s in sets:
        cs = load_set(s, cfg, a.limit)
        if not cs:
            print(f"[경고] 세트 {s} 에 문항이 없어요 (골든셋은 사람이 작성해야 해요)")
        cases += cs
    if a.case_ids:
        want = set(a.case_ids.split(","))
        cases = [c for c in cases if c["case_id"] in want]
    for c in cases:                     # 표에 없는 expected_behavior 는 여기서 멈춘다 (UnknownBehaviorLabel)
        bm.to7(c["expected_behavior"])
    if not cases:
        sys.exit("실행할 문항이 없어요")

    mode = cfg["retrieval"]["mode"]
    needs_ai = (not a.retrieval_only) or mode in ("vector", "hybrid")
    client = make_client(cfg, offline=a.dry_run) if needs_ai else None
    from src.retrieval.search import Retriever
    retriever = Retriever.from_config(cfg, client)
    id2pages = {u["source_id"]: (u["file"], u["pages"]) for u in read_jsonl(data_dir(cfg) / "sources.jsonl")}
    chunk_stats = {"n_chunks": len(retriever.chunks)}
    write_config(out_dir, exp_id, cfg, a.dry_run, sets, a.retrieval_only, chunk_stats)
    print(f"[{exp_id}] pipeline={cfg['pipeline']} mode={mode} k={cfg['retrieval']['k']} 문항 {len(cases)}개 "
          f"({'dry-run' if a.dry_run else '실제 AI'}{', 검색만' if a.retrieval_only else ''})")
    for s in {tuple(c["scope"]) for c in cases}:          # 스레드 전에 BM25 색인을 만들어 둔다
        if mode in ("bm25", "hybrid"):
            retriever._bm25_for(list(s))

    def work(case):
        t0 = time.time()
        try:
            out = retrieval_only(case, retriever, cfg) if a.retrieval_only else run_one(case, retriever, client, cfg)
            err = None
        except Exception as e:  # noqa: BLE001 - 한 문항 실패가 전체 실행을 멈추지 않게 기록하고 이어 간다
            out = {"answer": "", "behavior": "", "citations": [], "injection": False, "clean_query": "",
                   "retrieved": [], "queries": [], "trace": [], "usage": {"llm_calls": 0, "cached_calls": 0,
                   "search_calls": 0, "tokens_in": 0, "tokens_out": 0, "elapsed": round(time.time() - t0, 3)}}
            err = f"{type(e).__name__}: {e}"
        return case, out, err

    results_f = open(out_dir / "results.jsonl", "w", encoding="utf-8")
    scores_f = open(out_dir / "scores.jsonl", "w", encoding="utf-8")
    rows, errors = [], 0
    t_all = time.time()
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
        for n, (case, out, err) in enumerate(pool.map(work, cases), start=1):
            rr = RunResult(exp_id=exp_id, case_id=case["case_id"], set=case["set"], question=case["question"],
                           scope=case["scope"], pipeline=cfg["pipeline"], answer=out["answer"],
                           behavior=out["behavior"], citations=out["citations"], injection=out.get("injection", False),
                           clean_query=out.get("clean_query", ""),
                           retrieved=[{"rank": i + 1, **{k: r[k] for k in ("chunk_id", "file", "pages", "score", "text",
                                                                        "source_ids")}}
                                      for i, r in enumerate(out["retrieved"])],
                           queries=out["queries"], trace=out["trace"], usage=out["usage"], error=err).to_dict()
            if err:
                errors += 1
                print(f"  [오류] {case['case_id']}: {err}")
            results_f.write(json.dumps(rr, ensure_ascii=False) + "\n")
            try:
                row = score_case(case, out, bm, id2pages, client, cfg, with_answer=not a.retrieval_only)
            except Exception as e:  # noqa: BLE001
                row = {"case_id": case["case_id"], "set": case["set"], "usage": out["usage"], "retrieval": None,
                       "failure": "error", "error": f"{type(e).__name__}: {e}"}
                errors += 1
                print(f"  [채점 오류] {case['case_id']}: {e}")
            if err:
                row["failure"], row["error"] = "error", err
            scores_f.write(json.dumps(row, ensure_ascii=False) + "\n")
            results_f.flush()
            scores_f.flush()
            rows.append(row)
            if n % 20 == 0 or n == len(cases):
                print(f"  {n}/{len(cases)} ({time.time() - t_all:.0f}s)")
    results_f.close()
    scores_f.close()

    summary = {"exp_id": exp_id, "pipeline": cfg["pipeline"], "dry_run": a.dry_run, "retrieval_only": a.retrieval_only,
               "errors": errors, "overall": M.aggregate(rows),
               "by_set": {s: M.aggregate([r for r in rows if r["set"] == s]) for s in sets if any(r["set"] == s for r in rows)},
               "by_category": {c: M.aggregate([r for r in rows if r.get("category") == c])
                               for c in sorted({r.get("category") for r in rows if r.get("category")})},
               "judge_cost": {"calls": sum(r.get("judge_usage", {}).get("llm_calls", 0) for r in rows),
                              "tokens_in": sum(r.get("judge_usage", {}).get("tokens_in", 0) for r in rows),
                              "tokens_out": sum(r.get("judge_usage", {}).get("tokens_out", 0) for r in rows)}}
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    o = summary["overall"]
    with open(runs / "experiments.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"exp_id": exp_id, "date": dt.datetime.now().isoformat(timespec="seconds"),
                            "pipeline": cfg["pipeline"], "dry_run": a.dry_run, "sets": sets, "n": o["n"],
                            "changed": diff_config(cfg), "headline": {k: o.get(k) for k in (
                                "id_hit5", "id_mrr", "page_hit5", "answer_score", "behavior_match7", "behavior_match5",
                                "over_refusal_rate", "citation_correct", "injection_handled_rate")},
                            "cost": o["cost"]}, ensure_ascii=False) + "\n")
    print(f"\n요약 → {out_dir / 'summary.json'}")
    print(json.dumps({k: o.get(k) for k in ("n", "retrieval_n", "id_hit1", "id_hit5", "id_recall5", "id_mrr", "page_hit1",
                                              "page_hit5", "page_recall5", "page_mrr", "answer_score", "behavior_match7",
                                              "behavior_match5", "failures")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
