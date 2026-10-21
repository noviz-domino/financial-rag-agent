"""설정·behavior_map·스키마·Gemini 클라이언트(캐시·재시도·집계)."""
import json
from pathlib import Path

import pytest

from src.common.config import BehaviorMap, UnknownBehaviorLabel, diff_config, load_config
from src.common.llm import CachedClient, FakeBackend, LLMError, use_meter
from src.common.schemas import BEHAVIORS, SchemaError, check_chunk, check_page, parse_answer, parse_judge
from tests.conftest import mk_chunk

ROOT = Path(__file__).resolve().parents[1]


def test_behavior_map_covers_every_label_in_answers():
    bm = BehaviorMap.load()
    labels = set()
    for f in (ROOT / "03_answers").glob("*.jsonl"):
        for l in f.read_text(encoding="utf-8").splitlines():
            labels.add(json.loads(l)["expected_behavior"])
    assert len(labels) == 23                                   # negative 22종 + positive 1종
    for lab in labels:
        assert bm.to7(lab) in BEHAVIORS
    assert len(set(bm.map) - {"answer_with_citation"}) == 22


def test_behavior_map_exact_table_and_groups():
    bm = BehaviorMap.load()
    assert bm.to7("ignore_injection_and_preserve_provenance") == "answer_qualified"      # NEG034
    assert bm.to7("abstain_and_request_context") == "clarify"                            # NEG009
    assert bm.to7("flag_stale_source_and_verify") == "defer_freshness"                   # NEG037
    assert bm.to7("refuse_and_warn") == "refuse"
    assert bm.to5("answer_qualified") == "answer" and bm.to5("defer_freshness") == "abstain"
    assert bm.to5("correct") == "correct"


def test_behavior_map_unknown_label_fails_loudly():
    bm = BehaviorMap.load()
    with pytest.raises(UnknownBehaviorLabel):
        bm.to7("some_new_label")
    with pytest.raises(UnknownBehaviorLabel):
        bm.to5("nope")


def test_config_diff_only_changed_items():
    cfg = load_config(ROOT / "configs/exp2b_bm25.json")
    assert diff_config(cfg) == {"retrieval.mode": {"baseline": "hybrid", "value": "bm25"}}
    assert diff_config(load_config(ROOT / "configs/baseline.json")) == {}
    d = diff_config(load_config(ROOT / "configs/exp3_agent.json"))
    assert list(d) == ["pipeline"]


def test_each_experiment_changes_one_element():
    for f, n_keys in (("exp0_repeat", 0), ("exp2a_vector", 1), ("exp2b_bm25", 1), ("exp3_agent", 1),
                      ("exp4_embed_prefix", 1), ("exp5_k3", 1), ("exp5_k4", 1), ("exp6_embed2", 1),
                      ("exp1s_structural_800", 1), ("exp1s_structural_1600", 1),
                      ("exp1a_recursive_300_100", 3), ("exp1a_recursive_500_100", 2), ("exp1a_recursive_800_100", 3),
                      ("exp1b_parentchild_300", 3), ("exp1b_parentchild_400", 2), ("exp1b_parentchild_600", 3)):
        assert len(diff_config(load_config(ROOT / f"configs/{f}.json"))) == n_keys, f


def test_chunk_variant_names_do_not_collide():
    from src.common.config import chunk_variant_name
    names = {}
    for f in sorted((ROOT / "configs").glob("exp*.json")) + [ROOT / "configs/baseline.json"]:
        cfg = load_config(f)
        names.setdefault(chunk_variant_name(cfg), set()).add(json.dumps(cfg["chunking"], sort_keys=True))
    assert all(len(v) == 1 for v in names.values()), names      # 같은 이름 = 같은 청킹 설정 (파일을 덮어쓰지 않음)
    assert {"structural", "structural_800", "structural_1600", "recursive_300_100", "recursive_500_100",
            "recursive_800_100", "parentchild_300", "parentchild_400", "parentchild_600"} <= set(names)


def test_embedding2_uses_instruction_not_task_type():
    from src.common.llm import embed_instruction, embed_uses_instruction
    assert not embed_uses_instruction("gemini-embedding-001") and embed_uses_instruction("gemini-embedding-2")
    assert embed_instruction("gemini-embedding-001", "RETRIEVAL_QUERY", "질문") == "질문"
    assert embed_instruction("gemini-embedding-2", "RETRIEVAL_QUERY", "질문") == "task: search result | query: 질문"
    assert embed_instruction("gemini-embedding-2", "RETRIEVAL_DOCUMENT", "본문") == "title: none | text: 본문"


def test_embedding_cache_is_separated_by_model(tmp_path):
    c = _client(tmp_path)
    _, n1 = c.embed(["가나다"], "gemini-embedding-001", 32)
    _, n2 = c.embed(["가나다"], "gemini-embedding-2", 32)
    _, n3 = c.embed(["가나다"], "gemini-embedding-2", 32)
    assert (n1, n2, n3) == (1, 1, 0)
    v1, _ = c.embed(["가나다"], "gemini-embedding-001", 32, "RETRIEVAL_QUERY")
    v2, _ = c.embed(["가나다"], "gemini-embedding-2", 32, "RETRIEVAL_QUERY")
    assert not (v1 == v2).all()                              # 2 는 지시문이 붙어 다른 벡터


def test_schema_validation():
    with pytest.raises(SchemaError):
        parse_judge({"behavior": "maybe"})
    j = parse_judge({"behavior": "answer", "use_chunk_ids": ["a"], "injection": 1})
    assert j["injection"] is True and j["clean_query"] == ""
    with pytest.raises(SchemaError):
        parse_answer({"answer": "x", "behavior": "answer", "citations": [{"file": "a.pdf"}]})
    a = parse_answer({"answer": "x", "behavior": "refuse", "citations": [{"file": "a.pdf", "page": "3"}]})
    assert a["citations"][0]["page"] == 3
    for page in ("8,9", "8-9", "p.8", [8, 9]):                 # F15: 여러 쪽이면 첫 쪽 (예전엔 ValueError 로 문항 오류)
        a = parse_answer({"answer": "x", "behavior": "answer", "citations": [{"file": "a.pdf", "page": page}]})
        assert a["citations"][0]["page"] == 8
    with pytest.raises(SchemaError):                            # 숫자가 없으면 형식 오류 → 안전하게 처리
        parse_answer({"answer": "x", "behavior": "answer", "citations": [{"file": "a.pdf", "page": "없음"}]})
    c = mk_chunk("c", "a.pdf", "terms", "t")
    assert check_chunk(c)
    bad = dict(c)
    bad.pop("links")
    with pytest.raises(SchemaError):
        check_chunk(bad)
    with pytest.raises(SchemaError):
        check_page({"file": "a"})


def _client(tmp_path, handlers=None, retries=2):
    return CachedClient(FakeBackend(handlers), tmp_path, retries=retries, sleep=lambda s: None)


def test_client_meter_counts_calls_and_tokens(tmp_path):
    c = _client(tmp_path)
    with use_meter() as m:
        c.call("rewrite", "m", "프롬프트", ctx={"question": "질문"})
        c.call("rewrite", "m", "프롬프트2", ctx={"question": "질문"})
    assert m.usage.llm_calls == 2 and m.usage.tokens_in > 0 and m.usage.tokens_out > 0


class Flaky:
    name, cacheable = "flaky", True

    def __init__(self, fail_n):
        self.n, self.fail_n = 0, fail_n

    def generate(self, model, prompt, system, json_mode, images, temperature, max_tokens, ctx=None):
        self.n += 1
        if self.n <= self.fail_n:
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        return json.dumps({"ok": 1}), 10, 5, 0.0

    def embed(self, *a):
        raise AssertionError


def test_client_retry_and_disk_cache(tmp_path):
    b = Flaky(fail_n=2)
    c = CachedClient(b, tmp_path, retries=3, sleep=lambda s: None)
    r = c.call("t", "m", "p")
    assert r.data == {"ok": 1} and b.n == 3 and not r.cached           # 429 두 번 재시도 뒤 성공
    r2 = c.call("t", "m", "p")
    assert r2.cached and b.n == 3                                      # 캐시 → API 호출 없음
    with use_meter() as m:
        c.call("t", "m", "p")
    assert m.usage.llm_calls == 1 and m.usage.cached_calls == 1 and m.usage.tokens_in == 10   # 캐시도 사용량으로 센다


def test_client_gives_up_after_retries(tmp_path):
    c = CachedClient(Flaky(fail_n=99), tmp_path, retries=1, sleep=lambda s: None)
    with pytest.raises(LLMError):
        c.call("t", "m", "p")


def test_embedding_cache_embeds_only_changed_texts(tmp_path):
    c = _client(tmp_path)
    v1, n1 = c.embed(["가나다", "라마바"], "m", 32)
    v2, n2 = c.embed(["가나다", "라마바", "새 텍스트"], "m", 32)
    assert n1 == 2 and n2 == 1 and v2.shape == (3, 32)
    assert (v1 == v2[:2]).all()


def test_real_backend_requires_key(monkeypatch):
    from src.common.llm import GeminiBackend
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(LLMError):
        GeminiBackend()


def test_embedding_count_mismatch_is_an_error(tmp_path):
    class Bad(FakeBackend):
        cacheable = False

        def embed(self, model, texts, dim, task_type):      # gemini-embedding-2 가 여러 입력을 하나로 합쳐 돌려주던 상황
            return [[1.0] * dim]
    c = CachedClient(Bad(), tmp_path, retries=0, sleep=lambda s: None)
    with pytest.raises(LLMError):
        c.embed(["가", "나", "다"], "m", 8)
