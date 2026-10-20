"""데이터 형식 고정 (기획서 8장 1단계). 쪽 레코드·청크·검색 결과·판단/답변 JSON·실행 결과.

dict(JSONL) 로 저장하고, 여기서는 필드 이름과 검증 함수를 한곳에 둔다.
쪽(page) = PDF 쪽 순서 1부터. 펼침면 반쪽도 page 는 같고 half(L/R)만 붙는다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

DOC_TYPES = ("glossary", "terms", "life_guide", "additional")
BEHAVIORS = ("answer", "answer_qualified", "correct", "clarify", "abstain", "defer_freshness", "refuse")
GROUPS5 = ("answer", "correct", "clarify", "abstain", "refuse")
CONTENT_TYPES = ("text", "table", "figure")
FIGURE_KINDS = ("numeric_chart", "table", "diagram", "screenshot", "illustration", "decorative")
# 근거를 인용하지 않고 짧게만 답하는 행동 (기획서 3-6)
NO_ANSWER_BEHAVIORS = ("abstain", "clarify", "refuse", "defer_freshness")

PAGE_FIELDS = ("file", "doc_type", "page", "half", "printed_page", "text", "tables", "figure_lines", "has_text_layer")
CHUNK_FIELDS = ("chunk_id", "text", "embed_text", "file", "doc_type", "page", "pages", "half", "section",
                "content_type", "source", "source_ids", "parent_id", "links")
SEARCH_RESULT_FIELDS = ("chunk_id", "parent_id", "text", "file", "pages", "score", "content_type")


class SchemaError(ValueError):
    pass


def check_fields(d: dict, fields: tuple, what: str) -> dict:
    missing = [f for f in fields if f not in d]
    if missing:
        raise SchemaError(f"{what}: 필드 누락 {missing}")
    return d


def check_page(d):
    check_fields(d, PAGE_FIELDS, "page record")
    if d["doc_type"] not in DOC_TYPES:
        raise SchemaError(f"doc_type 이상: {d['doc_type']}")
    if d["half"] not in (None, "L", "R"):
        raise SchemaError("half 는 null/L/R")
    if not isinstance(d["page"], int) or d["page"] < 1:
        raise SchemaError("page 는 1부터 시작하는 정수")
    return d


def check_chunk(d):
    check_fields(d, CHUNK_FIELDS, "chunk")
    if d["content_type"] not in CONTENT_TYPES:
        raise SchemaError(f"content_type 이상: {d['content_type']}")
    if not isinstance(d["links"], dict):
        raise SchemaError("links 는 dict(prev/next/article_refs/figure_refs)")
    return d


def check_search_result(d):
    return check_fields(d, SEARCH_RESULT_FIELDS, "search result")


def parse_judge(d: dict) -> dict:
    """① 근거 판단 JSON: {behavior, injection, clean_query, use_chunk_ids, missing_info, reason}"""
    if not isinstance(d, dict):
        raise SchemaError("judge JSON 이 객체가 아님")
    beh = d.get("behavior")
    if beh not in BEHAVIORS:
        raise SchemaError(f"behavior 값 이상: {beh!r}")
    return {"behavior": beh, "injection": bool(d.get("injection", False)),
            "clean_query": (d.get("clean_query") or "").strip(),
            "use_chunk_ids": [str(x) for x in (d.get("use_chunk_ids") or [])],
            "missing_info": str(d.get("missing_info") or ""), "reason": str(d.get("reason") or "")}


def parse_answer(d: dict) -> dict:
    """② 답변 JSON: {answer, behavior, citations:[{file,page,chunk_id}], reason}"""
    if not isinstance(d, dict):
        raise SchemaError("answer JSON 이 객체가 아님")
    beh = d.get("behavior")
    if beh not in BEHAVIORS:
        raise SchemaError(f"behavior 값 이상: {beh!r}")
    cites = []
    for c in d.get("citations") or []:
        if not isinstance(c, dict) or "file" not in c or "page" not in c:
            raise SchemaError(f"citation 형식 이상: {c!r}")
        cites.append({"file": str(c["file"]), "page": int(c["page"]), "chunk_id": c.get("chunk_id")})
    return {"answer": str(d.get("answer") or ""), "behavior": beh, "citations": cites,
            "reason": str(d.get("reason") or "")}


@dataclass
class Usage:
    """문항 하나의 비용 합계."""
    llm_calls: int = 0
    cached_calls: int = 0
    search_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    elapsed: float = 0.0

    def to_dict(self):
        return asdict(self)


@dataclass
class RunResult:
    """results.jsonl 한 줄."""
    exp_id: str
    case_id: str
    set: str
    question: str
    scope: list
    pipeline: str
    answer: str = ""
    behavior: str = ""
    citations: list = field(default_factory=list)
    injection: bool = False
    clean_query: str = ""
    retrieved: list = field(default_factory=list)   # [{rank, chunk_id, file, pages, score, text, source_ids}]
    queries: list = field(default_factory=list)
    trace: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
