"""RAG·Agent 가 함께 쓰는 AI 호출 단계 (같은 프롬프트·같은 모델 → 흐름만 비교). 각 호출은 task 이름으로 구분된다.

잘못된 JSON(스키마 위반)은 조용히 넘기지 않고 안전한 기본값(abstain)으로 처리하며 trace 에 parse_error 를 남긴다.
"""
from __future__ import annotations

from src.common.schemas import SchemaError, parse_answer, parse_judge
from src.generation import prompts as P

ANSWER_LIKE = ("answer", "answer_qualified", "correct")


def _call(client, cfg, task, prompt, ctx, model=None):
    lc = cfg["llm"]
    return client.call(task, model or lc["model"], prompt, system=P.SYSTEM, temperature=lc["temperature"],
                       max_tokens=lc.get("max_output_tokens", 2048), ctx=ctx)


def slim(results):
    return [{"chunk_id": r["chunk_id"], "file": r["file"], "pages": r["pages"], "text": r["text"],
             "source_ids": r.get("source_ids", [])} for r in results]


def judge_step(client, cfg, question, results, trace=None) -> dict:
    """① 근거 판단: {behavior, injection, clean_query, use_chunk_ids, missing_info, reason}"""
    r = _call(client, cfg, "judge", P.judge_prompt(question, results), {"question": question, "results": slim(results)})
    try:
        return parse_judge(r.data)
    except SchemaError as e:
        if trace is not None:
            trace.append({"node": "judge", "parse_error": str(e)})
        return {"behavior": "abstain", "injection": False, "clean_query": "", "use_chunk_ids": [],
                "missing_info": "", "reason": f"판단 JSON 오류: {e}"}


def grade_step(client, cfg, question, results, trace=None) -> dict:
    """Agent 의 grade: 검색 결과를 보고 한 번에 판단 (judge 형식 + sufficient, link_hint).
    답변류 행동이 아니면 sufficient 는 항상 False (재검색 또는 정형 응답으로 간다)."""
    r = _call(client, cfg, "grade", P.grade_prompt(question, results), {"question": question, "results": slim(results)})
    try:
        out = parse_judge(r.data)
    except SchemaError as e:
        if trace is not None:
            trace.append({"node": "grade", "parse_error": str(e)})
        out = {"behavior": "abstain", "injection": False, "clean_query": "", "use_chunk_ids": [], "missing_info": "",
               "reason": f"grade JSON 오류: {e}"}
        return {**out, "sufficient": False, "link_hint": False}
    d = r.data
    return {**out, "sufficient": out["behavior"] in ANSWER_LIKE and bool(d.get("sufficient", True)),
            "link_hint": bool(d.get("link_hint"))}


def answer_step(client, cfg, question, behavior, results, extra="", trace=None) -> dict:
    """② 답변: {answer, behavior, citations, reason}"""
    r = _call(client, cfg, "answer", P.answer_prompt(question, behavior, results, extra),
              {"question": question, "behavior": behavior, "results": slim(results), "extra": extra})
    try:
        return parse_answer(r.data)
    except SchemaError as e:
        if trace is not None:
            trace.append({"node": "answer", "parse_error": str(e)})
        return {"answer": "", "behavior": behavior, "citations": [], "reason": f"답변 JSON 오류: {e}"}


def select_context(results, use_ids):
    """판단 단계가 고른 근거만 남긴다. 고른 것이 하나도 없으면(id 불일치 포함) 전체를 쓴다."""
    if use_ids:
        picked = [r for r in results if r["chunk_id"] in set(use_ids)]
        if picked:
            return picked
    return results
