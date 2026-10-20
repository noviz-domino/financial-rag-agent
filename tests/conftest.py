"""공통 테스트 도우미. 테스트는 네트워크·API 를 절대 쓰지 않는다 (FakeBackend / 스텁만)."""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.pop("GEMINI_API_KEY", None)      # 실수로라도 실제 키로 호출되지 않게


@pytest.fixture
def cfg():
    from src.common.config import load_config
    return load_config(ROOT / "configs" / "baseline.json")


def mk_chunk(cid, file, doc_type, text, pages=(1,), source_ids=(), content_type="text", **kw):
    from src.chunking.common import make_chunk
    return make_chunk(cid, file, doc_type, list(pages), "", text, content_type=content_type,
                      source_ids=list(source_ids), **kw)


class StubRetriever:
    """Agent 테스트용: search 호출을 세고 미리 정한 결과를 돌려준다."""

    def __init__(self, results_by_query=None, default=None):
        self.results_by_query = results_by_query or {}
        self.default = default if default is not None else []
        self.queries = []

    def search(self, query, k=5, *, scope=None, mode=None):
        from src.common.llm import current_meter
        m = current_meter()
        if m:
            m.add_search()
        self.queries.append(query)
        return self.results_by_query.get(query, self.default)

    def get_parent(self, chunk_id):
        return None

    def follow_ref(self, chunk_id):
        return []


def result(cid="c1", text="휴면성 증권계좌는 6개월 이상 거래가 없는 계좌예요 10만원", file="a.pdf", pages=(3,), dt="glossary"):
    return {"chunk_id": cid, "parent_id": None, "text": text, "file": file, "pages": list(pages), "score": 1.0,
            "content_type": "text", "doc_type": dt, "section": "", "source_ids": [f"src-{cid}"], "half": None}


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
