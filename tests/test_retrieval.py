"""검색: RRF 융합, scope 강제, 모드, parent/ref 도구. 임베딩은 가짜(오프라인)."""
import copy

import pytest

from src.common.llm import CachedClient, FakeBackend, use_meter
from src.retrieval.search import Retriever, ScopeRequiredError, normalize_scope, rrf_fuse
from tests.conftest import mk_chunk


def test_rrf_fusion_math_and_order():
    out = dict(rrf_fuse([["a", "b", "c"], ["b", "c", "d"]], k=60))
    assert out["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert out["a"] == pytest.approx(1 / 61) and out["d"] == pytest.approx(1 / 63)
    ranked = [i for i, _ in rrf_fuse([["a", "b", "c"], ["b", "c", "d"]])]
    assert ranked == ["b", "c", "a", "d"]                 # 두 목록에 모두 있는 b, c 가 위로


def test_rrf_single_list_keeps_order():
    assert [i for i, _ in rrf_fuse([["x", "y", "z"]])] == ["x", "y", "z"]


def test_scope_required():
    for bad in (None, [], ""):
        with pytest.raises(ScopeRequiredError):
            normalize_scope(bad)
    with pytest.raises(ScopeRequiredError):
        normalize_scope(["not_a_doc_type"])
    assert normalize_scope("base") == ["glossary", "terms"]
    assert normalize_scope(["terms", "terms"]) == ["terms"]


def _corpus():
    g = mk_chunk("g1", "gloss.pdf", "glossary", "신용거래융자 증권회사가 투자자에게 주식 매수 자금을 빌려주는 제도", source_ids=["fsc-glossary-1"])
    t1 = mk_chunk("t1", "04.pdf", "terms", "제15조(청약철회) 신용거래 청약철회는 14일 이내에 할 수 있다", source_ids=["kofia-1"],
                  parent_id="P1", article_no=15, chunk_index=0)
    t2 = mk_chunk("t2", "04.pdf", "terms", "제16조(담보) 담보로 제공된 증권의 반환 절차를 정한다", source_ids=["kofia-2"], parent_id="P1")
    t1["links"]["article_refs"] = [{"label": "제16조", "target": "t2"}]
    lg = mk_chunk("l1", "guide.pdf", "life_guide", "연금저축 세액공제 한도는 연 600만원이다", source_ids=["fss-life-guide-pdf-3"])
    parents = [{"chunk_id": "P1", "text": "제15조 전체 + 제16조 전체 본문", "file": "04.pdf", "doc_type": "terms", "pages": [1, 2],
                "half": None, "section": "04", "child_ids": ["t1", "t2"]}]
    return [g, t1, t2, lg], parents


def _retriever(cfg, mode="bm25", fold=False):
    c = copy.deepcopy(cfg)
    c["retrieval"]["mode"], c["retrieval"]["parent_fold"] = mode, fold
    chunks, parents = _corpus()
    client = CachedClient(FakeBackend(), __import__("pathlib").Path(__import__("tempfile").mkdtemp()))
    vec = None
    if mode != "bm25":
        class V:                                    # 가짜 임베딩 코사인 검색 (Chroma 없이)
            def query(self, text, n, scope):
                import numpy as np
                q, _ = client.embed([text], "m", 64, "RETRIEVAL_QUERY")
                docs = [x for x in chunks if x["doc_type"] in scope]
                m, _ = client.embed([x["text"] for x in docs], "m", 64)
                s = m @ q[0]
                return [(docs[i]["chunk_id"], float(s[i])) for i in np.argsort(-s)[:n]]
        vec = V()
    return Retriever(c, chunks, parents, client, vec)


def test_search_scope_filters_doc_types(cfg):
    r = _retriever(cfg)
    res = r.search("연금저축 세액공제", 5, scope=["life_guide"])
    assert [x["chunk_id"] for x in res] == ["l1"]
    res = r.search("연금저축 세액공제 한도 청약철회", 5, scope=["glossary", "terms"])
    assert all(x["doc_type"] in ("glossary", "terms") for x in res)       # 범위 밖(life_guide)은 절대 안 나옴
    with pytest.raises(ScopeRequiredError):
        r.search("질문")
    with pytest.raises(ScopeRequiredError):
        r.search("질문", scope=None)


def test_search_result_fields_and_search_count(cfg):
    r = _retriever(cfg)
    with use_meter() as m:
        res = r.search("청약철회 14일", 2, scope=["terms"])
    assert m.usage.search_calls == 1
    for k in ("chunk_id", "parent_id", "text", "file", "pages", "score", "content_type"):
        assert k in res[0]
    assert res[0]["chunk_id"] == "t1" and res[0]["parent_id"] == "P1"


@pytest.mark.parametrize("mode", ["vector", "hybrid", "bm25"])
def test_modes_return_k(cfg, mode):
    r = _retriever(cfg, mode)
    res = r.search("신용거래 청약철회", 2, scope=["glossary", "terms"])
    assert 1 <= len(res) <= 2


def test_parent_and_ref_tools(cfg):
    r = _retriever(cfg)
    p = r.get_parent("t1")
    assert p["chunk_id"] == "P1" and "전체" in p["text"] and p["pages"] == [1, 2]
    assert r.get_parent("g1") is None
    refs = r.follow_ref("t1")
    assert [x["chunk_id"] for x in refs] == ["t2"] and refs[0]["ref_label"] == "제16조"
    assert r.follow_ref("l1") == []


def test_chroma_index_build_cache_and_query(cfg, tmp_path):
    """Chroma 색인(가짜 임베딩): 같은 내용은 다시 임베딩하지 않고, where(doc_type) 로 범위가 강제된다."""
    from src.index.store import VectorIndex, build_index
    c = copy.deepcopy(cfg)
    c["paths"]["data"] = str(tmp_path)
    client = CachedClient(FakeBackend(), tmp_path / "cache")
    chunks, _ = _corpus()
    build_index(c, chunks, client, log=lambda *_: None)
    n_before = client.backend.calls.__len__()
    msgs = []
    build_index(c, chunks, client, log=msgs.append)
    assert "새로 임베딩 호출한 0개" in msgs[0]                    # 두 번째 색인은 캐시만 사용
    vi = VectorIndex(c, client)
    ids = [i for i, _ in vi.query("연금저축 세액공제", 4, ["life_guide"])]
    assert ids == ["l1"]
    assert {i for i, _ in vi.query("신용거래", 4, ["glossary", "terms"])} <= {"g1", "t1", "t2"}


def test_parent_fold_dedupes_children(cfg):
    r = _retriever(cfg, fold=True)
    res = r.search("신용거래 청약철회 담보 반환", 5, scope=["terms"])
    assert len(res) == 1 and res[0]["chunk_id"] == "P1" and "전체" in res[0]["text"]
    assert set(res[0]["source_ids"]) == {"kofia-1", "kofia-2"}
