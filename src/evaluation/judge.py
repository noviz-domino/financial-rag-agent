"""AI 채점 (답을 만든 모델과 다른 judge_model, 결과 캐시). 한 문항에 호출 1회:
답변 점수 0~2 + AI 가 판정할 must_include/must_not 항목 + 환각 여부."""
from __future__ import annotations

from src.common.llm import use_meter
from src.generation import prompts as P


def score_answer(client, cfg: dict, case: dict, answer: str, retrieved: list[dict], ai_include: list[str],
                 ai_not: list[str]) -> dict:
    ctx_text = "\n".join(f"[{r['file']} p.{r['pages'][0]}] {r['text'][:400]}" for r in retrieved[:5]) or "(없음)"
    prompt = P.SCORE_PROMPT.format(question=case["question"], reference=case.get("reference_answer", ""),
                                   answer=answer or "(빈 답변)", must_include=ai_include or "(없음)",
                                   must_not=ai_not or "(없음)", context=ctx_text)
    with use_meter() as m:
        r = client.call("score", cfg["llm"]["judge_model"], prompt, temperature=0, max_tokens=cfg["llm"].get("max_output_tokens", 8192),
                        ctx={"answer": answer, "must_include_ai": ai_include, "must_not_ai": ai_not})
    d = r.data if isinstance(r.data, dict) else {}
    try:
        score = max(0, min(2, int(d.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    inc = {x.get("item"): bool(x.get("present")) for x in d.get("must_include", []) if isinstance(x, dict)}
    neg = {x.get("item"): bool(x.get("violated")) for x in d.get("must_not", []) if isinstance(x, dict)}
    return {"score": score, "include": {it: inc.get(it, False) for it in ai_include},
            "violated": {it: neg.get(it, False) for it in ai_not}, "hallucination": bool(d.get("hallucination")),
            "reason": str(d.get("reason") or ""), "usage": m.finish()}
