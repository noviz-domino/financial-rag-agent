"""평가 문항 로딩: 질문(02_questions) + 기준 답안(03_answers/*.jsonl)을 case_id 로 연결, 세트별 검색 범위(scope) 부여.

골든셋(05_golden/golden_set.jsonl)은 한 줄에 질문까지 들어 있고, 문항마다 scope 를 가진다 (비어 있는 파일이 정상).
"""
from __future__ import annotations

import json
from pathlib import Path

from src.common.config import ROOT

SETS = {
    "positive": {"questions": ["positive_questions.json"], "answers": ["positive_cases.jsonl"],
                 "scope": ["glossary", "terms"]},
    "negative": {"questions": ["negative_questions.json"], "answers": ["negative_cases.jsonl"],
                 "scope": ["glossary", "terms"]},
    "life_guide": {"questions": ["positive_life_guide_questions.json"], "answers": ["positive_life_guide_cases.jsonl"],
                   "scope": ["life_guide"]},
    "additional": {"questions": ["housing_pension_2026_questions.json", "housing_policy_finance_2025_questions.json",
                                 "insurance_terms_example_2020_questions.json",
                                 "policy_finance_debt_adjustment_2026_questions.json",
                                 "retirement_pension_2025_questions.json", "self_employed_debt_2025_questions.json"],
                   "answers": ["additional_cases.jsonl"], "scope": ["additional"]},
}
GOLDEN_REQUIRED = ("case_id", "question", "reference_answer", "expected_behavior", "scope")


def read_jsonl(path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def _paths(cfg):
    p = cfg.get("paths", {})
    root = lambda k, d: (ROOT / p.get(k, d)) if not Path(p.get(k, d)).is_absolute() else Path(p[k])
    return root("questions", "02_questions"), root("answers", "03_answers"), root("golden", "05_golden/golden_set.jsonl")


def load_set(name: str, cfg: dict, limit: int | None = None) -> list[dict]:
    qdir, adir, gpath = _paths(cfg)
    if name == "golden":
        cases = read_jsonl(gpath) if gpath.exists() else []
        for c in cases:
            miss = [k for k in GOLDEN_REQUIRED if k not in c]
            if miss:
                raise ValueError(f"골든셋 {c.get('case_id')}: 필수 필드 누락 {miss}")
            c.setdefault("set", "golden")
            c.setdefault("expected_source_ids", [])
            c.setdefault("must_include", [])
            c.setdefault("must_not_include", [])
        return cases[:limit] if limit else cases
    if name not in SETS:
        raise KeyError(f"알 수 없는 세트: {name} (가능: {list(SETS)} + golden)")
    spec = SETS[name]
    answers = {}
    for f in spec["answers"]:
        for a in read_jsonl(adir / f):
            answers[a["case_id"]] = a
    cases = []
    for f in spec["questions"]:
        with open(qdir / f, encoding="utf-8") as fh:
            for q in json.load(fh):
                a = answers.get(q["case_id"])
                if a is None:
                    raise KeyError(f"{q['case_id']}: 기준 답안 없음")
                cases.append({**a, "question": q["question"], "case_id": q["case_id"], "set": name,
                              "scope": list(spec["scope"])})
    return cases[:limit] if limit else cases


def load_all_answers(cfg: dict) -> list[dict]:
    """03_answers 의 모든 기준 답안 (정답 근거 연결 검증용)."""
    _, adir, _ = _paths(cfg)
    out = []
    for f in sorted(adir.glob("*.jsonl")):
        out += read_jsonl(f)
    return out
