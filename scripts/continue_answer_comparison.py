"""Continue the frozen batch, counting documented JSON output failures as incorrect.

This does not regenerate answers or alter their scores. Transport/quota errors,
missing cases and different source hashes still block the comparison.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import evaluate_answer_candidates as batch  # noqa: E402


def verified(run_dir, expected_ids):
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        raise RuntimeError(f"Incomplete run preserved: {run_dir}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    scores = batch.read_jsonl(run_dir / "scores.jsonl")
    results = batch.read_jsonl(run_dir / "results.jsonl")
    cfg = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    if summary["dry_run"] or summary["retrieval_only"] or cfg.get("judge_context_version") != batch.JUDGE_CONTEXT_VERSION:
        raise RuntimeError(f"Invalid evaluation: {run_dir}")
    accepted = []
    for rows in (scores, results):
        if len(rows) != len(expected_ids) or {r["case_id"] for r in rows} != expected_ids:
            raise RuntimeError(f"Case coverage differs: {run_dir}")
        for r in rows:
            error = r.get("error")
            if error:
                if "LLMError: answer 호출 실패: Unterminated string" not in error:
                    raise RuntimeError(f"Unresolved non-format error: {run_dir}: {error}")
                accepted.append({"case_id": r["case_id"], "error": error})
    score_by_id = {r["case_id"]: r for r in scores}
    if any(score_by_id[r["case_id"]].get("score") != 0 for r in accepted):
        raise RuntimeError("Output failures must be counted as zero, not omitted")
    if any(r.get("score") not in (0, 1, 2) for r in scores):
        raise RuntimeError(f"Invalid scores: {run_dir}")
    if summary["errors"] and not accepted:
        raise RuntimeError(f"Unaccounted error count: {run_dir}")
    if accepted:
        batch.dump(run_dir / "documented_output_failures.json", {
            "policy": "Repeated model JSON-output failure retained as incorrect; all 180 cases remain",
            "failures": list({r["case_id"]: r for r in accepted}.values()),
            "raw_files_modified": False,
        })
    return summary, scores


if __name__ == "__main__":
    target = ROOT / "runs" / "answer_rank_20261022"
    batch.dump(target / "output_failure_policy.json", {
        "date": "2026-10-22", "observed_case": "exp1a_recursive_300_100 / POS049",
        "reason": "Same JSON format failure on original and unchanged-config retry; exclusion would bias accuracy",
        "rule": "Keep malformed model answers at score zero in full denominator; retain original errors and retry",
        "unchanged": ["questions", "reference answers", "candidate settings", "models", "prompts", "scoring code"],
        "transport_errors_still_block": True,
        "continuation_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    })
    batch.verified_summary = verified
    batch.main()
