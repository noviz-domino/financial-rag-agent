"""LangGraph Agent (기획서 4장). 구조도와 노드·갈래가 1:1 이다.

START → retrieve → grade ─┬ refuse ─────────→ END          (부적절한 요청)
           ↑              ├ clarify ────────→ END          (조건 부족)
           │              ├ defer_freshness → END          (최신 정보)
           │              ├ 충분 ───────────────→ generate
           │              ├ 링크로 보완(2차) → expand → generate
           └── rewrite ←──┤ 부족 & 재검색 < 2   (인젝션이면 지시를 뺀 질문으로)
                          └ 부족 & 재검색 = 2 → abstain → END
          generate → verify ─┬ 통과 → END
                             ├ 실패 & 재생성 < 1 → generate
                             └ 실패 & 재생성 = 1 → abstain → END
모든 판단은 검색 결과를 본 뒤 grade 가 한 번에 한다 (D34: 검색 전 route 판단은 과다 거절 0.32 를 만들어 없앴다).
AI 호출 상한 7 (grade 3 + rewrite 2 + generate 2): 넘으면 그 자리에서 abstain.
verify 는 규칙만(AI 호출 없음). 검색 도구·AI·프롬프트는 기본 RAG 와 같다.
refuse/clarify/defer_freshness/abstain 응답은 AI 없이 grade 의 이유로 만든 정형 문장이다 (호출 수 상한을 지키기 위해).
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from src.agent.verify import verify_answer
from src.common.llm import current_meter, use_meter
from src.generation import prompts as P
from src.generation.steps import ANSWER_LIKE, _call, answer_step, grade_step, select_context

NODES = ["retrieve", "grade", "rewrite", "expand", "generate", "verify", "refuse", "clarify",
         "defer_freshness", "abstain"]
TERMINAL = ("refuse", "clarify", "defer_freshness")   # grade 가 이 행동으로 판단하면 재검색 없이 바로 정형 응답


class AgentState(TypedDict, total=False):
    case_id: str
    question: str
    scope: list
    injection: bool
    clean_query: str
    queries: list
    retrieved: list
    context: list
    grade: dict
    missing_info: str
    retry_count: int
    regen_count: int
    verify_errors: list
    answer: str
    behavior: str
    citations: list
    reason: str
    trace: list
    llm_calls: int
    search_calls: int
    tokens_in: int
    tokens_out: int
    elapsed: float
    cap_hit: bool
    expanded: bool


def _sync(state: dict) -> dict:
    """집계값을 state 에 반영 (현재 문항의 Meter 기준)."""
    m = current_meter()
    if m:
        u = m.usage
        return {"llm_calls": u.llm_calls, "search_calls": u.search_calls, "tokens_in": u.tokens_in,
                "tokens_out": u.tokens_out}
    return {}


def build_graph(retriever, client, cfg: dict):
    ac = cfg["agent"]
    k = cfg["retrieval"]["k"]
    max_calls, max_retry, max_regen = ac["max_llm_calls"], ac["max_retrieval_retries"], ac["max_regenerate"]

    def calls() -> int:
        m = current_meter()
        return m.usage.llm_calls if m else 0

    def q_of(s):
        return s["clean_query"] if s.get("injection") and s.get("clean_query") else s["question"]

    def log(s, **kw):
        return s.get("trace", []) + [kw]

    # ---- 노드
    def retrieve(s):
        q = s["queries"][-1]
        res = retriever.search(q, k, scope=s["scope"])
        return {"retrieved": res, "context": res, "expanded": False,
                "trace": log(s, node="retrieve", query=q, chunk_ids=[r["chunk_id"] for r in res]), **_sync(s)}

    def grade(s):
        if calls() >= max_calls:
            return {"cap_hit": True, "trace": log(s, node="grade", note="AI 호출 상한"), **_sync(s)}
        trace = list(s.get("trace", []))
        g = grade_step(client, cfg, q_of(s), s["retrieved"], trace)
        # 인젝션은 첫 판단에서 한 번 찾으면 유지 (재검색 뒤의 판단은 이미 지시를 뺀 질문을 보므로 다시 못 찾는다)
        injection = bool(s.get("injection")) or g["injection"]
        clean_query = s.get("clean_query") or g["clean_query"]
        return {"grade": g, "injection": injection, "clean_query": clean_query, "missing_info": g["missing_info"],
                "reason": g["reason"], "trace": trace + [{"node": "grade", **g}], **_sync(s)}

    def rewrite(s):
        if calls() >= max_calls:
            return {"cap_hit": True, "trace": log(s, node="rewrite", note="AI 호출 상한"), **_sync(s)}
        q = q_of(s)
        r = _call(client, cfg, "rewrite", P.rewrite_prompt(q, s["queries"], s.get("missing_info", "")),
                  {"question": q, "queries": s["queries"], "missing_info": s.get("missing_info", "")})
        d = r.data if isinstance(r.data, dict) else {}
        nq = str(d.get("query") or "").strip() or q
        return {"queries": s["queries"] + [nq], "retry_count": s["retry_count"] + 1,
                "trace": log(s, node="rewrite", query=nq), **_sync(s)}

    def expand(s):
        """링크 따라가기(2차): 근거 청크의 조항·그림 참조 대상과 parent 를 컨텍스트에 보충 (설정 agent.expand)."""
        ctx = list(s["context"])
        have = {c["chunk_id"] for c in ctx}
        base = select_context(s["retrieved"], (s.get("grade") or {}).get("use_chunk_ids", []))[:3]
        added = []
        for r in base:
            for t in retriever.follow_ref(r["chunk_id"]) + ([retriever.get_parent(r["chunk_id"])] if r.get("parent_id") else []):
                if t and t["chunk_id"] not in have and len(added) < 3:
                    have.add(t["chunk_id"])
                    added.append(t)
        return {"context": ctx + added, "expanded": True,
                "trace": log(s, node="expand", added=[a["chunk_id"] for a in added]), **_sync(s)}

    def generate(s):
        if calls() >= max_calls:
            return {"cap_hit": True, "trace": log(s, node="generate", note="AI 호출 상한"), **_sync(s)}
        q = q_of(s)
        g_beh = (s.get("grade") or {}).get("behavior")
        beh = g_beh if g_beh in ANSWER_LIKE else "answer"
        ctx = select_context(s["context"], (s.get("grade") or {}).get("use_chunk_ids", []))
        extra = P.REGEN_EXTRA.format(errors="; ".join(s["verify_errors"])) if s.get("verify_errors") else ""
        a = answer_step(client, cfg, q, beh, ctx, extra, s.get("trace"))
        regen = s.get("regen_count", 0) + (1 if s.get("verify_errors") else 0)   # 검사 실패 뒤의 재생성이면 +1
        return {"answer": a["answer"], "behavior": a["behavior"], "citations": a["citations"], "context": s["context"],
                "reason": a["reason"], "regen_count": regen,
                "trace": log(s, node="generate", behavior=a["behavior"], regen=regen), **_sync(s)}

    def verify(s):
        ctx = select_context(s["context"], (s.get("grade") or {}).get("use_chunk_ids", []))
        errs = verify_answer(s["answer"], s["behavior"], s["citations"], ctx, q_of(s))
        return {"verify_errors": errs, "trace": log(s, node="verify", errors=errs, passed=not errs)}

    def _final(node, behavior, text):
        def f(s):
            return {"answer": text(s), "behavior": behavior, "citations": [],
                    "trace": log(s, node=node, behavior=behavior), **_sync(s)}
        return f

    why = lambda s: (s.get("reason") or "").strip()
    refuse = _final("refuse", "refuse", lambda s: f"요청하신 내용에는 응할 수 없어요. {why(s)}".strip())
    clarify = _final("clarify", "clarify", lambda s: f"답변하려면 추가 정보가 필요해요. {why(s)} 어떤 상품·조건인지 알려 주세요.".strip())
    defer = _final("defer_freshness", "defer_freshness",
                   lambda s: f"최신 정보는 제공된 문서로 확인할 수 없어요. 공식 출처에서 확인해 주세요. {why(s)}".strip())

    def abstain_text(s):
        if s.get("cap_hit"):
            why_ = "AI 호출 상한에 도달했어요."
        elif s.get("verify_errors"):
            why_ = "답변의 근거 확인에 실패했어요: " + "; ".join(s["verify_errors"])
        else:
            why_ = "검색 결과에서 충분한 근거를 찾지 못했어요." + (f" (부족한 정보: {s['missing_info']})" if s.get("missing_info") else "")
        return f"제공된 문서만으로는 답할 수 없어요. {why_} 공식 자료를 확인해 주세요."
    abstain = _final("abstain", "abstain", abstain_text)

    # ---- 분기
    def after_grade(s):
        if s.get("cap_hit"):
            return "exhausted_or_cap"
        g = s["grade"]
        if g["behavior"] in TERMINAL:
            return g["behavior"]
        if g["sufficient"]:
            return "sufficient"
        if g["link_hint"] and ac.get("expand") and not s.get("expanded"):
            return "link"
        return "retry" if s["retry_count"] < max_retry else "exhausted_or_cap"

    def after_rewrite(s):
        return "cap" if s.get("cap_hit") else "ok"

    def after_generate(s):
        return "cap" if s.get("cap_hit") else "ok"

    def after_verify(s):
        if not s["verify_errors"]:
            return "pass"
        return "regen" if s["regen_count"] < max_regen else "fail"

    g = StateGraph(AgentState)
    for name, fn in (("retrieve", retrieve), ("grade", grade), ("rewrite", rewrite), ("expand", expand),
                     ("generate", generate), ("verify", verify), ("refuse", refuse), ("clarify", clarify),
                     ("defer_freshness", defer), ("abstain", abstain)):
        g.add_node(name, fn)
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "grade")
    g.add_conditional_edges("grade", after_grade, {"refuse": "refuse", "clarify": "clarify",
                                                   "defer_freshness": "defer_freshness", "sufficient": "generate",
                                                   "link": "expand", "retry": "rewrite", "exhausted_or_cap": "abstain"})
    g.add_conditional_edges("rewrite", after_rewrite, {"ok": "retrieve", "cap": "abstain"})
    g.add_edge("expand", "generate")
    g.add_conditional_edges("generate", after_generate, {"ok": "verify", "cap": "abstain"})
    g.add_conditional_edges("verify", after_verify, {"pass": END, "regen": "generate", "fail": "abstain"})
    for n in ("refuse", "clarify", "defer_freshness", "abstain"):
        g.add_edge(n, END)
    return g.compile()


def export_mermaid(graph) -> str:
    return graph.get_graph().draw_mermaid()


def run_agent(case: dict, retriever, client, cfg: dict) -> dict:
    graph = build_graph(retriever, client, cfg)
    init: AgentState = {"case_id": case["case_id"], "question": case["question"], "scope": case["scope"],
                        "queries": [case["question"]], "retry_count": 0, "regen_count": 0, "verify_errors": [],
                        "trace": [], "cap_hit": False, "expanded": False, "retrieved": [], "context": []}
    with use_meter() as meter:
        out = graph.invoke(init, config={"recursion_limit": 40})
        usage = meter.finish()
    return {"answer": out.get("answer", ""), "behavior": out.get("behavior", ""), "citations": out.get("citations", []),
            "injection": out.get("injection", False), "clean_query": out.get("clean_query", ""),
            "retrieved": out.get("retrieved", []), "queries": out.get("queries", []), "trace": out.get("trace", []),
            "usage": usage, "reason": out.get("reason", ""), "state": {k: out.get(k) for k in (
                "retry_count", "regen_count", "verify_errors", "missing_info", "grade")}}
