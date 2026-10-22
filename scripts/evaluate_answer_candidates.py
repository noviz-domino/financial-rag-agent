"""Freeze 15 existing candidates, rank on provided 180 cases, then test top 3 on golden.

Real API execution requires --execute. Existing complete runs are verified and reused;
partial runs are never overwritten. All raw records stay in runs/.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.common.config import load_config  # noqa: E402
from src.evaluation.dataset import load_set, read_jsonl  # noqa: E402
from src.evaluation.judge import JUDGE_CONTEXT_VERSION  # noqa: E402

CANDIDATES = [
    "baseline", "exp1a_recursive_300_100", "exp1a_recursive_500_100", "exp1a_recursive_800_100",
    "exp1b_parentchild_300", "exp1b_parentchild_400", "exp1b_parentchild_600",
    "exp1s_structural_800", "exp1s_structural_1600", "exp2a_vector", "exp2b_bm25",
    "exp4_embed_prefix", "exp5_k3", "exp5_k4", "exp6_embed2",
]
SETS = ["positive", "negative", "life_guide", "additional"]


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verified_summary(run_dir, expected_ids):
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        raise RuntimeError(f"Incomplete run preserved: {run_dir}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    scores = read_jsonl(run_dir / "scores.jsonl")
    results = read_jsonl(run_dir / "results.jsonl")
    cfg = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    if (summary["dry_run"] or summary["retrieval_only"] or summary["errors"]
            or cfg.get("judge_context_version") != JUDGE_CONTEXT_VERSION):
        raise RuntimeError(f"Invalid evaluation: {run_dir}")
    for rows in (scores, results):
        if len(rows) != len(expected_ids) or {r["case_id"] for r in rows} != expected_ids:
            raise RuntimeError(f"Case coverage differs: {run_dir}")
        if any(r.get("error") for r in rows):
            raise RuntimeError(f"Case error: {run_dir}")
    if any(r.get("score") not in (0, 1, 2) for r in scores):
        raise RuntimeError(f"Invalid answer scores: {run_dir}")
    return summary, scores


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--batch-id", default="answer_rank_20261022")
    ap.add_argument("--config-workers", type=int, default=3)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    batch = ROOT / "runs" / args.batch_id
    if not args.execute:
        print(json.dumps({"candidates": CANDIDATES, "provided_cases": 180,
                          "provided_evaluations": len(CANDIDATES) * 180,
                          "golden_evaluations": 63, "pipeline": "agent",
                          "judge_context_version": JUDGE_CONTEXT_VERSION}, ensure_ascii=False, indent=2))
        return
    batch.mkdir(parents=True, exist_ok=True)
    base = load_config(ROOT / "configs" / "baseline.json")
    provided = [c for name in SETS for c in load_set(name, base)]
    golden = load_set("golden", base)
    assert len(provided) == 180 and len(golden) == 21
    provided_ids = {c["case_id"] for c in provided}
    golden_ids = {c["case_id"] for c in golden}
    hashed_paths = list((ROOT / "src").rglob("*.py")) + list((ROOT / "configs").glob("*.json"))
    hashed_paths += list((ROOT / "02_questions").glob("*.json"))
    hashed_paths += list((ROOT / "03_answers").glob("*.jsonl"))
    hashed_paths += [ROOT / "05_golden" / "golden_set.jsonl", Path(__file__)]
    hashes = {str(p.relative_to(ROOT)).replace("\\", "/"): sha(p) for p in sorted(hashed_paths)}
    manifest = {"date": "2026-10-22", "timezone": "Asia/Seoul", "candidates": CANDIDATES,
                "pipeline": "agent", "sets": SETS, "provided_ids": sorted(provided_ids),
                "golden_ids": sorted(golden_ids), "judge_context_version": JUDGE_CONTEXT_VERSION,
                "workers": args.workers, "config_workers": args.config_workers,
                "ranking": ["sum(answer score 0-2) descending", "count(score=2) descending",
                            "hallucination count ascending", "input tokens ascending", "candidate name"],
                "hashes": hashes, "golden_previously_evaluated": True,
                "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()}
    mp = batch / "manifest.json"
    if mp.exists():
        old = json.loads(mp.read_text(encoding="utf-8"))
        if old != manifest:
            raise RuntimeError("Frozen manifest differs; use a new batch ID, do not mix versions")
    else:
        dump(mp, manifest)

    def run(candidate, phase):
        cfg = copy.deepcopy(load_config(ROOT / "configs" / f"{candidate}.json"))
        cfg["pipeline"] = "agent"
        cfg["evaluation"]["sets"] = SETS if phase == "provided" else ["golden"]
        exp_id = f"{args.batch_id}_{phase}_{candidate}"
        cfg["exp_id"] = exp_id
        config_path = batch / "configs" / f"{phase}_{candidate}.json"
        if config_path.exists() and json.loads(config_path.read_text(encoding="utf-8")) != cfg:
            raise RuntimeError(f"Frozen config differs: {config_path}")
        dump(config_path, cfg)
        run_dir = ROOT / "runs" / exp_id
        expected = provided_ids if phase == "provided" else golden_ids
        if not run_dir.exists():
            log = batch / f"{phase}_{candidate}.log"
            with log.open("w", encoding="utf-8") as stream:
                cp = subprocess.run([sys.executable, "-u", "-m", "src.evaluation.run", "--config", str(config_path),
                                     "--sets", ",".join(cfg["evaluation"]["sets"]), "--workers", str(args.workers)],
                                    cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                    stdout=stream, stderr=subprocess.STDOUT)
            if cp.returncode:
                raise RuntimeError(f"Evaluation failed: {exp_id}; see {log}")
        summary, scores = verified_summary(run_dir, expected)
        row = {"candidate": candidate, "phase": phase, "exp_id": exp_id, "summary": summary,
               "score_sum": sum(r["score"] for r in scores), "full_correct": sum(r["score"] == 2 for r in scores),
               "hallucinations": sum(bool(r["hallucination"]) for r in scores)}
        print(f"DONE {phase} {candidate}: n={len(scores)}, answer_score={summary['overall']['answer_score']}", flush=True)
        return row

    def execute(names, phase):
        rows, errors = [], []
        with ThreadPoolExecutor(max_workers=max(1, args.config_workers)) as pool:
            futures = {pool.submit(run, c, phase): c for c in names}
            for future in as_completed(futures):
                try:
                    rows.append(future.result())
                except Exception as exc:
                    errors.append({"candidate": futures[future], "error": str(exc)})
                    print(f"ERROR {phase} {futures[future]}: {exc}", flush=True)
                dump(batch / f"{phase}_progress.json", {"completed": rows, "errors": errors})
        if errors:
            raise RuntimeError(f"{phase}: {len(errors)} candidates failed; no ranking or golden selection")
        return rows

    rows = execute(CANDIDATES, "provided")
    rows.sort(key=lambda r: (-r["score_sum"], -r["full_correct"], r["hallucinations"],
                            r["summary"]["overall"]["cost"]["tokens_in"], r["candidate"]))
    selected = [r["candidate"] for r in rows[:3]]
    dump(batch / "ranking.json", {"ranking": rows, "selected": selected})
    print("TOP3 " + ", ".join(selected), flush=True)
    golden_rows = execute(selected, "golden")
    dump(batch / "golden_comparison.json", {"selected_on_provided": selected, "results": golden_rows})
    assert all(sha(ROOT / p) == h for p, h in hashes.items()), "Evaluation inputs changed during execution"
    print(f"COMPLETE {batch}", flush=True)


if __name__ == "__main__":
    main()
