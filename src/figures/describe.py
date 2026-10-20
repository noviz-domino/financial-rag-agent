"""그림 → 글 (Gemini 1회 호출, JSON {kind, description}) + 숫자·길이 자동 검사. 기획서 3-3.

종류(kind)는 같은 호출에서 Gemini 가 먼저 고른다:
  numeric_chart · table · diagram  → 표 형식으로 값을 전부
  screenshot · illustration        → 짧은 요약 (안 보이는 글자는 쓰지 않음)
  decorative                       → 청크를 만들지 않음
"""
from __future__ import annotations

import re

from src.common.schemas import FIGURE_KINDS

FULL_KINDS = ("numeric_chart", "table", "diagram")
NUM = re.compile(r"-?\d[\d,]*\.?\d*")

PROMPT = """이 이미지는 한국어 금융 문서에서 잘라낸 그림·차트·표 영역입니다. 검색용 글로 바꿉니다.

먼저 kind 를 하나 고르세요: numeric_chart | table | diagram | screenshot | illustration | decorative
- numeric_chart(숫자가 적힌 그래프), table(표), diagram(절차도·구조도): description 에 Markdown 표나 글머리표로 **값을 전부** 적습니다.
- screenshot(화면 캡처), illustration(일러스트·작은 글자): description 에 **짧게 요약**(3~5문장)하고, 흐려서 안 보이는 글자는 쓰지 않습니다.
- decorative(장식·배경·로고): description 은 빈 문자열.

규칙
1. 그림에 글자로 적혀 있는 숫자만 씁니다. 선·막대 높이를 눈대중으로 읽은 값, 추정값은 쓰지 않습니다.
2. 단위는 축·표에 적힌 그대로 씁니다 (백만원을 억 원으로 바꾸지 않습니다).
3. 기관명·상품명·항목명은 이미지에 적힌 글자 그대로 쓰고 바꾸거나 보완하지 않습니다.
4. 범례가 있으면 범례를 먼저 적고 각 값이 어느 계열인지 연결합니다. 범례가 잘렸으면 '(계열 불명)'으로 둡니다.
5. 합쳐진 칸의 값은 해당하는 모든 칸에 채웁니다 (예: '600만원'이 '이하/초과' 두 칸에 걸치면 두 칸 모두).
6. 출력은 {max_chars}자를 넘지 않게 합니다. 같은 내용을 반복하지 않습니다.
7. 마지막 줄에 '핵심:' 한 문장 (보이는 내용에서만).

참고용 주변 캡션: {caption}
참고용 OCR 글자(틀릴 수 있음): {ocr}

JSON 하나만 출력: {{"kind": "...", "description": "..."}}"""


def _digits(n):
    return re.sub(r"[,.]", "", n).lstrip("-")


def _found(n, ref):
    # 축에는 '17, 19'로, 설명에는 '2017년'으로 쓰는 경우: 4자리 연도는 뒤 두 자리가 원문에 있으면 인정
    return n in ref or (re.fullmatch(r"(19|20)\d\d", n) is not None and n[2:] in ref)


def unverified_numbers(desc: str, reference: str) -> list[str]:
    """설명에 쓰인 숫자 중 기준 텍스트(텍스트 레이어·OCR·캡션)에 없는 것. 눈대중 추정값일 가능성."""
    ref = {_digits(n) for n in NUM.findall(reference or "")}
    out = []
    for n in NUM.findall(desc or ""):
        if len(_digits(n)) >= 2 and not _found(_digits(n), ref) and n not in out:
            out.append(n)
    return out


def number_check(desc: str, reference: str):
    """설명 숫자 중 기준 텍스트에 실제로 있는 비율 (0~1). 비교할 숫자가 없으면 None."""
    ref = {_digits(n) for n in NUM.findall(reference or "")}
    got = [_digits(n) for n in NUM.findall(desc or "") if len(_digits(n)) >= 2]
    if not got or not ref:
        return None
    return round(sum(1 for n in got if _found(n, ref)) / len(got), 3)


def length_flag(desc: str, max_chars: int) -> str | None:
    """길이 검사: 상한 초과(고장 난 반복 출력)·같은 줄 과다 반복이면 이유를 돌려준다."""
    if len(desc) > max_chars:
        return "too_long"
    lines = [l.strip() for l in desc.split("\n") if len(l.strip()) > 5]
    if len(lines) >= 8 and len(set(lines)) / len(lines) < 0.4:
        return "repeated_lines"
    return None


def describe_figure(client, model: str, fig: dict, max_chars: int = 4000, max_tokens: int = 4096) -> dict:
    """그림 1개 → {kind, description, tokens...}. 호출은 캐시(이미지 해시+프롬프트+모델)됨."""
    prompt = PROMPT.format(max_chars=max_chars, caption=fig.get("caption") or "없음",
                           ocr=(fig.get("ocr_text") or "없음")[:600].replace("\n", " "))
    r = client.call("figure", model, prompt, images=[fig["_png"]], max_tokens=max_tokens,
                    ctx={"figure": {k: fig.get(k) for k in ("figure_id", "kind", "caption", "text_layer", "ocr_text")}})
    data = r.data if isinstance(r.data, dict) else {}
    kind = data.get("kind") if data.get("kind") in FIGURE_KINDS else "illustration"
    return {"gemini_kind": kind, "description": str(data.get("description") or "").strip(),
            "tokens_in": r.tokens_in, "tokens_out": r.tokens_out, "cached": r.cached}


def apply_checks(fig: dict, max_chars: int):
    """숫자 검사·길이 검사 → num_check, unverified_numbers, length_flag, caution, review_reasons."""
    desc = fig.get("description") or ""
    ref = "\n".join([fig.get("text_layer") or "", fig.get("ocr_text") or "", fig.get("caption") or ""])
    fig["num_check"] = number_check(desc, ref)
    fig["unverified_numbers"] = unverified_numbers(desc, ref)
    fig["length_flag"] = length_flag(desc, max_chars)
    fig["caution"] = bool(fig["unverified_numbers"])          # '인용 주의' 표시
    reasons = []
    if fig["length_flag"]:
        reasons.append(fig["length_flag"])
    if fig["num_check"] is not None and fig["num_check"] < 0.8:
        reasons.append("low_num_check")
    if fig.get("gemini_kind") == "screenshot":
        reasons.append("screenshot")                          # 화면 캡처는 사람이 확인
    fig["review_reasons"] = reasons
    return fig
