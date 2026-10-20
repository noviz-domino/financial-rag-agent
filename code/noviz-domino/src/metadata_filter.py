"""메타데이터 필터 (docs/청크_구조_선택지.md 의 G).

1) to_index_metadata(chunk) : 벡터 DB 저장용으로 평탄화
     Chroma 등은 메타데이터 값으로 str/int/float/bool 만 받는 경우가 많아 list·dict·None 을 변환한다.
2) Filter                   : 같은 조건을 두 형태로 만든다
     .where()      → Chroma `where` 딕셔너리 (벡터 검색)
     .match(chunk) → 파이썬 판정 함수 (BM25·후처리)
3) suggest_filter(question, scope) : 질문 문구로 고르는 규칙 기반 필터 제안 (Agent 라우터의 기본값)
     규칙이 틀리면 정답을 통째로 놓치므로, 기본은 '추천(soft)'이고 hard=True 일 때만 강제한다.
"""
import json
import re
from dataclasses import dataclass, field

SCOPE_DOC_TYPES = {
    "base": ["glossary", "terms"],
    "life_guide": ["life_guide"],
    "additional": ["additional"],
    "all": ["glossary", "terms", "life_guide", "additional"],
}
FLAT_KEYS = ["chunk_id", "strategy", "doc_id", "doc_type", "file", "doc_title", "page", "half", "printed_page",
             "chunk_index", "content_type", "figure_kind", "source", "level", "parent_id", "prev_id", "next_id",
             "title", "caption", "image_path", "num_check", "article_no"]


def to_index_metadata(chunk: dict) -> dict:
    md = {k: chunk[k] for k in FLAT_KEYS if chunk.get(k) is not None}
    md["pages"] = ",".join(map(str, chunk.get("pages", [])))
    md["source_ids"] = ",".join(chunk.get("source_ids", []))
    md["section_path"] = " > ".join(x for x in chunk.get("section_path", []) if x)
    md["n_refs"] = len(chunk.get("refs", []))
    if chunk.get("refs"):
        md["refs"] = json.dumps(chunk["refs"], ensure_ascii=False)  # 문자열로 보관, 조회 시 json.loads
    return md


@dataclass
class Filter:
    doc_types: list = field(default_factory=list)
    content_types: list = field(default_factory=list)
    files: list = field(default_factory=list)
    reason: str = ""

    def _conds(self):
        conds = []
        for key, vals in (("doc_type", self.doc_types), ("content_type", self.content_types), ("file", self.files)):
            if vals:
                conds.append({key: {"$in": list(vals)}})
        return conds

    def where(self):
        c = self._conds()
        return None if not c else (c[0] if len(c) == 1 else {"$and": c})

    def match(self, chunk):
        return ((not self.doc_types or chunk.get("doc_type") in self.doc_types) and
                (not self.content_types or chunk.get("content_type") in self.content_types) and
                (not self.files or chunk.get("file") in self.files))


TABLE_HINT = re.compile(r"표(에서|의|를|로)?\s|표에|몇\s*(%|퍼센트|원|명|건)|비중|비율|추이|증가율|금액은|얼마")
FIGURE_HINT = re.compile(r"그래프|차트|그림|도표|추세|막대|원형")
TERMS_HINT = re.compile(r"약관|제\d+조|조항|계좌설정|신용거래|대차거래|환매조건부|전자금융")
GLOSSARY_HINT = re.compile(r"(이란|란|이라는 것은)\s*(무엇|뭐)|의 (뜻|정의|의미)|용어")


def suggest_filter(question: str, scope: str = "base", hard: bool = False) -> Filter:
    """scope 의 doc_type 은 항상 적용(과제 조건: 세트별 검색 범위 고정). 나머지는 규칙으로 제안."""
    f = Filter(doc_types=list(SCOPE_DOC_TYPES[scope]))
    reasons = [f"scope={scope}"]
    if scope == "base":
        if TERMS_HINT.search(question) and not GLOSSARY_HINT.search(question):
            f.doc_types, _ = ["terms"], reasons.append("약관 단서")
        elif GLOSSARY_HINT.search(question) and not TERMS_HINT.search(question):
            f.doc_types, _ = ["glossary"], reasons.append("용어 정의 단서")
    else:
        if FIGURE_HINT.search(question):
            f.content_types, _ = ["figure", "table"], reasons.append("그림 단서")
        elif TABLE_HINT.search(question):
            f.content_types, _ = ["table", "figure", "text"], reasons.append("수치 단서(표 우선, 본문 포함)")
    if not hard:  # soft: 범위(doc_type)만 강제하고 나머지는 '제안'으로만 기록
        f.reason = "; ".join(reasons) + " (soft)"
        f.content_types = [] if f.content_types == ["table", "figure", "text"] else f.content_types
        if f.doc_types != SCOPE_DOC_TYPES[scope]:
            f.reason += f" 제안 doc_types={f.doc_types}"
            f.doc_types = list(SCOPE_DOC_TYPES[scope])
        if f.content_types:
            f.reason += f" 제안 content_types={f.content_types}"
            f.content_types = []
    else:
        f.reason = "; ".join(reasons) + " (hard)"
    return f
