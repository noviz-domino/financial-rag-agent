"""Retry only failed completed evaluation cases without changing inputs or hiding originals."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.evaluation.dataset import read_jsonl  # noqa: E402
from src.evaluation import metrics as M  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run")
    ap.add_argument("--attempt", type=int, default=1)
    a = ap.parse_args()
    d = ROOT / "runs" / a.run
    record = json.loads((d / "config.json").read_text(encoding="utf-8"))
    old_summary = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    scores = read_jsonl(d / "scores.jsonl")
    results = read_jsonl(d / "results.jsonl")
    failed = {r["case_id"] for r in scores if r.get("error")}
    failed |= {r["case_id"] for r in results if r.get("error")}
    if not failed:
        print("No errors to retry")
        return
    retry_id = f"{a.run}_retry{a.attempt}"
    config = d / f"retry{a.attempt}_config.json"
    cfg = record["config"]
    if config.exists():
        assert json.loads(config.read_text(encoding="utf-8")) == cfg
    else:
        config.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    retry_dir = ROOT / "runs" / retry_id
    if not retry_dir.exists():
        cp = subprocess.run([sys.executable, "-u", "-m", "src.evaluation.run", "--config", str(config),
                             "--sets", ",".join(record["sets"]), "--case-ids", ",".join(sorted(failed)),
                             "--exp-id", retry_id, "--workers", "2"], cwd=ROOT,
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if cp.returncode:
            raise RuntimeError("Retry command failed; originals unchanged")
    new_scores = {r["case_id"]: r for r in read_jsonl(retry_dir / "scores.jsonl")}
    new_results = {r["case_id"]: r for r in read_jsonl(retry_dir / "results.jsonl")}
    if set(new_scores) != failed or set(new_results) != failed:
        raise RuntimeError("Retry case coverage differs; originals unchanged")
    if any(r.get("error") for r in [*new_scores.values(), *new_results.values()]):
        raise RuntimeError("Retry still has errors; originals unchanged")
    for name in ("results.jsonl", "scores.jsonl", "summary.json"):
        backup = d / f"{name}.before_retry{a.attempt}"
        if backup.exists():
            raise RuntimeError(f"Backup exists, refusing overwrite: {backup}")
        shutil.copy2(d / name, backup)
    for r in new_results.values():
        r["exp_id"] = a.run
        r["recovered_from"] = retry_id
    scores = [new_scores.get(r["case_id"], r) for r in scores]
    results = [new_results.get(r["case_id"], r) for r in results]
    for name, rows in (("results.jsonl", results), ("scores.jsonl", scores)):
        (d / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    old_summary.update(errors=0, overall=M.aggregate(scores),
                       by_set={s: M.aggregate([r for r in scores if r["set"] == s]) for s in record["sets"]},
                       by_category={c: M.aggregate([r for r in scores if r.get("category") == c])
                                    for c in sorted({r.get("category") for r in scores if r.get("category")})},
                       judge_cost={"calls": sum(r.get("judge_usage", {}).get("llm_calls", 0) for r in scores),
                                   "tokens_in": sum(r.get("judge_usage", {}).get("tokens_in", 0) for r in scores),
                                   "tokens_out": sum(r.get("judge_usage", {}).get("tokens_out", 0) for r in scores)})
    old_summary.setdefault("retries", []).append({"run": retry_id, "case_ids": sorted(failed),
                                                 "unchanged_config": True, "originals_preserved": True})
    (d / "summary.json").write_text(json.dumps(old_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Recovered " + ", ".join(sorted(failed)) + "; pre-retry records preserved")


if __name__ == "__main__":
    main()
