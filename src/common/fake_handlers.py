"""오프라인 가짜 AI 의 기본 핸들러 (--dry-run·테스트). 규칙 기반이라 결정적이고, 실제 성능을 흉내 내려는 게 아니라
평가 루프·Agent 그래프·저장 형식이 끝까지 도는지 보는 용도다. 모든 핸들러는 ctx(dict) -> dict|str.
"""
from __future__ import annotations

import re

INJECTION = re.compile(r"(무시하고|무시해|SYSTEM\s*:|이전 지시|지시를 따르지|출처를? 달지|citation)", re.I)
REFUSE = re.compile(r"(추천|매수|매도|사야|사도 될|오를지|수익.*보장|우회|속여)")
FRESH = re.compile(r"(오늘|내일|현재|최근 5년|실시간|지금)")


def _clean_query(q: str) -> str:
    parts = re.split(r"[.!?\n]", q)
    kept = [p for p in parts if p.strip() and not INJECTION.search(p)]
    return (" ".join(kept) or q).strip()


def judge(ctx):
    q, res = ctx["question"], ctx.get("results", [])
    inj = bool(INJECTION.search(q))
    clean = _clean_query(q) if inj else ""
    base = clean or q
    if REFUSE.search(base):
        beh = "refuse"
    elif FRESH.search(base):
        beh = "defer_freshness"
    elif len(base) < 8:
        beh = "clarify"
    elif not res:
        beh = "abstain"
    else:
        beh = "answer"
    return {"behavior": beh, "injection": inj, "clean_query": clean, "use_chunk_ids": [r["chunk_id"] for r in res[:3]],
            "missing_info": "" if res else "근거 없음", "reason": "fake judge"}


def grade(ctx):
    """judge 와 같은 판단 + 질문 단어가 검색 결과에 하나라도 있으면 근거 충분."""
    res = ctx.get("results", [])
    out = judge(ctx)
    q_terms = set(re.findall(r"[가-힣A-Za-z0-9]{2,}", out["clean_query"] or ctx["question"]))
    hit = sum(1 for r in res if q_terms & set(re.findall(r"[가-힣A-Za-z0-9]{2,}", r["text"])))
    ok = out["behavior"] == "answer" and hit >= 1
    if out["behavior"] == "answer" and not ok:
        out["behavior"] = "abstain"
    return {**out, "sufficient": ok, "link_hint": False, "missing_info": "" if ok else "관련 근거 없음",
            "reason": "fake grade"}


def rewrite(ctx):
    return {"query": (ctx["question"] + " 정의").strip()}


def answer(ctx):
    beh, res = ctx["behavior"], ctx.get("results", [])
    if beh in ("answer", "answer_qualified", "correct") and res:
        r = res[0]
        cite = f"({r['file']} p.{r['pages'][0]})"
        return {"answer": f"{r['text'][:80].replace(chr(10), ' ')} {cite}", "behavior": beh,
                "citations": [{"file": r["file"], "page": r["pages"][0], "chunk_id": r["chunk_id"]}], "reason": "fake"}
    if beh in ("answer", "answer_qualified", "correct"):
        return {"answer": "근거를 찾지 못했어요.", "behavior": "abstain", "citations": [], "reason": "fake: 근거 없음"}
    msg = {"clarify": "어떤 상품·조건인지 알려 주세요.", "abstain": "제공된 문서에 없어 답할 수 없어요. 공식 자료를 확인하세요.",
           "defer_freshness": "최신 정보는 문서로 알 수 없어요. 공식 출처에서 확인하세요.",
           "refuse": "개인 맞춤 투자 권유나 예측은 할 수 없어요."}[beh]
    return {"answer": msg, "behavior": beh, "citations": [], "reason": "fake"}


def score(ctx):
    ans = re.sub(r"[\s,.]", "", ctx.get("answer", ""))
    inc = [{"item": it, "present": re.sub(r"[\s,.]", "", it) in ans} for it in ctx.get("must_include_ai", [])]
    return {"score": 1, "must_include": inc,
            "must_not": [{"item": it, "violated": False} for it in ctx.get("must_not_ai", [])],
            "hallucination": False, "reason": "fake judge"}


def figure(ctx):
    f = ctx["figure"]
    text = (f.get("ocr_text") or f.get("text_layer") or "")[:300]
    return {"kind": "numeric_chart" if text else "decorative", "description": text}


DEFAULT_HANDLERS = {"judge": judge, "grade": grade, "rewrite": rewrite, "answer": answer,
                    "score": score, "figure": figure}
