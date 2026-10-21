"""정답 근거 연결 검증: 03_answers 의 expected_source_ids 가 모두 근거 단위·청크에 연결되는가.

  python -m src.sources.check --config configs/baseline.json
누락 0 이 목표. 연결 뒤에는 기준 답안 must_include(짧은 항목)가 연결된 원문에 있는지 내용으로도 확인한다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from src.common.config import data_dir, load_config
from src.evaluation.dataset import load_all_answers, read_jsonl


def norm(s: str) -> str:
    return re.sub(r"[\s,.;:·ㆍ'\"()\[\]{}<>~\-_/\\|*]+", "", s or "").lower()


def check(units, chunks, answers, short=20):
    """반환: dict(total, missing_in_units, missing_in_chunks, keyword_miss)."""
    uid = {u["source_id"]: u for u in units}
    cover = {}
    for c in chunks:
        for sid in c["source_ids"]:
            cover.setdefault(sid, []).append(c)
    total, miss_u, miss_c, kw = 0, [], [], []
    for a in answers:
        for sid in a.get("expected_source_ids", []):
            total += 1
            if sid not in uid:
                miss_u.append((a["case_id"], sid))
            elif sid not in cover:
                miss_c.append((a["case_id"], sid))
        # 핵심어 확인: 정답 연결 청크 본문에 짧은 must_include 가 하나라도 있는지 (positive 문항만)
        ids = a.get("expected_source_ids", [])
        if a["case_id"].startswith("POS") and ids:   # ADD-* 의 must_include 는 의역이라 글자 대조가 의미 없음
            text = norm("".join(c["text"] for sid in ids for c in cover.get(sid, [])))
            items = [norm(m) for m in a.get("must_include", []) if len(m) < short]
            if items and not any(i in text for i in items):
                kw.append(a["case_id"])
    return {"total": total, "missing_in_units": miss_u, "missing_in_chunks": miss_c, "keyword_miss": kw}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/baseline.json")
    a = ap.parse_args()
    cfg = load_config(a.config)
    d = data_dir(cfg)
    units = read_jsonl(d / "sources.jsonl")
    from src.common.config import chunk_variant_name
    chunks = read_jsonl(d / "chunks" / f"{chunk_variant_name(cfg)}.jsonl")
    r = check(units, chunks, load_all_answers(cfg))
    print(f"expected_source_ids 총 {r['total']}개 | 근거 단위 누락 {len(r['missing_in_units'])} | "
          f"청크 누락 {len(r['missing_in_chunks'])} | 핵심어 불일치 문항 {len(r['keyword_miss'])}")
    for k in ("missing_in_units", "missing_in_chunks"):
        for x in r[k][:20]:
            print("  ", k, x)
    if r["keyword_miss"]:
        print("  핵심어 불일치(사람 확인):", r["keyword_miss"][:20])
    sys.exit(1 if r["missing_in_units"] or r["missing_in_chunks"] else 0)


if __name__ == "__main__":
    main()
