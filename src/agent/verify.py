"""verify 노드의 규칙 검사 (AI 호출 없음, 비용 0). 기획서 4장.

  ① 인용이 검색 결과에 있나: 답변 citations·본문 '(파일명 p.N)' 의 (파일, 쪽) 이 근거 청크의 (파일, 쪽) 에 있어야 함
  ② 숫자가 근거에 있나: 답변 속 2자리 이상 숫자가 근거 글(또는 질문)에 있어야 함
  ③ 근거를 쓰는 행동(answer/answer_qualified/correct)이면 인용이 1개 이상
근거를 인용하지 않는 행동(abstain/clarify/refuse/defer_freshness)은 통과.
"""
from __future__ import annotations

from src.evaluation.metrics import parse_citations, unsupported_numbers

CITING = ("answer", "answer_qualified", "correct")


def verify_answer(answer: str, behavior: str, citations: list[dict], context: list[dict], question: str = "") -> list[str]:
    """실패 이유 목록 (빈 목록이면 통과)."""
    if behavior not in CITING:
        return []
    errors = []
    cites = parse_citations(answer, citations)
    if not cites:
        errors.append("출처 (파일명 p.N) 가 없음")
    ctx_pages = {(r["file"], p) for r in context for p in r["pages"]}
    bad = [f"{f} p.{p}" for f, p in cites if (f, p) not in ctx_pages]
    if bad:
        errors.append("검색 결과에 없는 출처: " + ", ".join(bad))
    nums = unsupported_numbers(answer, " ".join(r["text"] for r in context), question)
    if nums:
        errors.append("근거에 없는 숫자: " + ", ".join(nums[:8]))
    return errors
