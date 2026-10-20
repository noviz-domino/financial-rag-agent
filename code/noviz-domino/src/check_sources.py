"""정답(03_answers/*.jsonl)의 expected_source_ids 가 units.jsonl 에 모두 존재하는지 검증.

검색 평가의 전제 조건 체크: 여기서 누락이 있으면 Recall 이 '구조적으로' 0 이 되는 문항이 생긴다.
실행:  python -m src.check_sources
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_jsonl(p):
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def main():
    units = {u["source_id"]: u for u in load_jsonl(ROOT / "data/processed/units.jsonl")}
    total, missing = 0, []
    for f in sorted((ROOT / "03_answers").glob("*.jsonl")):
        for case in load_jsonl(f):
            for sid in case.get("expected_source_ids", []):
                total += 1
                if sid not in units:
                    missing.append((case["case_id"], sid))
    print(f"expected_source_ids 총 {total}개, 누락 {len(missing)}개")
    for cid, sid in missing:
        print("  MISSING", cid, sid)

    # 샘플 확인: 근거 단위 내용이 정답과 맞는지 사람이 눈으로 대조하는 용도
    for sid in ["fsc-glossary-1697", "kofia-terms-125-1776-017", "housing_pension_2026-pdf-15"]:
        u = units.get(sid)
        if u:
            print(f"\n[{sid}] {u['file']} p.{u['pages']} | {u['title']}\n  {u['text'][:150]!r}")


if __name__ == "__main__":
    main()
