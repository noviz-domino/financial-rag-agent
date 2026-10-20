"""기본 RAG (기획서 3-6): 검색 → ① 근거 판단 → (인젝션이면 clean_query 로 다시 검색) → ② 답변. 항상 AI 2회.

인젝션: 질문 안의 지시는 판단 단계가 injection=true + clean_query 로 걸러 준다. 추가 AI 호출은 없고 검색만 한 번 더 한다.
근거를 인용하지 않는 행동(abstain/clarify/refuse/defer_freshness)도 답변 단계에서 그 행동에 맞는 짧은 응답을 쓴다.
"""
from __future__ import annotations

from src.common.llm import use_meter
from src.generation.steps import ANSWER_LIKE, answer_step, judge_step, select_context


def run_rag(case: dict, retriever, client, cfg: dict) -> dict:
    trace, queries = [], []
    k = cfg["retrieval"]["k"]
    q = case["question"]
    with use_meter() as meter:
        results = retriever.search(q, k, scope=case["scope"])
        queries.append(q)
        trace.append({"node": "retrieve", "query": q, "chunk_ids": [r["chunk_id"] for r in results]})
        judge = judge_step(client, cfg, q, results, trace)
        trace.append({"node": "judge", **{x: judge[x] for x in ("behavior", "injection", "clean_query", "reason")}})
        eff_q = q
        if judge["injection"] and judge["clean_query"] and judge["clean_query"] != q:
            eff_q = judge["clean_query"]
            results = retriever.search(eff_q, k, scope=case["scope"])
            queries.append(eff_q)
            trace.append({"node": "retrieve", "query": eff_q, "chunk_ids": [r["chunk_id"] for r in results],
                          "note": "injection → clean_query 로 다시 검색"})
        behavior = judge["behavior"]
        context = select_context(results, judge["use_chunk_ids"]) if behavior in ANSWER_LIKE else []
        ans = answer_step(client, cfg, eff_q, behavior, context, trace=trace)
        trace.append({"node": "answer", "behavior": ans["behavior"]})
        usage = meter.finish()
    return {"answer": ans["answer"], "behavior": ans["behavior"], "citations": ans["citations"],
            "injection": judge["injection"], "clean_query": judge["clean_query"], "retrieved": results,
            "queries": queries, "trace": trace, "usage": usage, "reason": ans["reason"]}
