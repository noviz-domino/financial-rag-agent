"""평가: 검색 지표(ID·쪽), must_include 분리 규칙, 행동 일치, 인용, 실패 분류, 실행 폴더 형식(dry-run 엔드투엔드)."""
import json

import pytest

from src.evaluation import metrics as M
from src.evaluation.dataset import load_set
from tests.conftest import result, write_jsonl


def R(cid, sids, file="a.pdf", pages=(1,)):
    return {"chunk_id": cid, "file": file, "pages": list(pages), "source_ids": list(sids)}


def test_retrieval_metrics_id_based():
    ret = [R("c1", ["x"]), R("c2", ["gold1"]), R("c3", ["gold2"]), R("c4", []), R("c5", []), R("c6", ["gold3"])]
    m = M.retrieval_case_metrics(ret, ["gold1", "gold2", "gold3"], set())
    assert m["id_hit1"] == 0.0 and m["id_hit5"] == 1.0 and m["id_mrr"] == 0.5
    assert m["id_recall5"] == pytest.approx(2 / 3)                       # gold3 는 6위라 Recall@5 에서 제외
    assert "page_hit5" not in m


def test_retrieval_metrics_page_based_and_empty_gold():
    ret = [R("c1", ["other"], "a.pdf", (7,)), R("c2", ["other2"], "a.pdf", (9, 10))]
    m = M.retrieval_case_metrics(ret, ["gold"], {("a.pdf", 10)})
    assert m["id_hit5"] == 0.0 and m["page_hit5"] == 1.0 and m["page_hit1"] == 0.0 and m["page_mrr"] == 0.5
    assert M.retrieval_case_metrics(ret, [], set()) is None                # expected_source_ids 가 비면 집계 제외


def test_aggregate_only_over_cases_with_gold():
    rows = [{"set": "p", "retrieval": {"id_hit5": 1.0}}, {"set": "p", "retrieval": None}, {"set": "p", "retrieval": {"id_hit5": 0.0}}]
    a = M.aggregate(rows)
    assert a["n"] == 3 and a["retrieval_n"] == 2 and a["id_hit5"] == 0.5


def test_must_include_split_rule_and_normalization():
    items = ["6개월 이상", "주택을 담보로 한 대출이며 상속 시 정산 절차가 필요하다는 설명", "corpus에 없음"]
    rule, ai = M.split_items(items, is_negative=False)
    assert rule == ["6개월 이상", "corpus에 없음"] and len(ai) == 1            # 20자 미만 = 규칙, 긴 문장 = AI
    rule, ai = M.split_items(items, is_negative=True)
    assert rule == [] and ai == items                                       # negative 는 전부 AI
    got = M.rule_contains("휴면 계좌는 6 개월, 이상 거래가 없어요.", ["6개월 이상", "10만원"])
    assert got == {"6개월 이상": True, "10만원": False}                       # 공백·쉼표·문장부호 무시


def test_behavior_match_7_and_5(cfg):
    from src.common.config import BehaviorMap
    bm = BehaviorMap.load()
    assert M.behavior_match("answer_qualified", "answer", bm.to5) == {"match7": False, "match5": True}
    assert M.behavior_match("defer_freshness", "abstain", bm.to5) == {"match7": False, "match5": True}
    assert M.behavior_match("refuse", "abstain", bm.to5) == {"match7": False, "match5": False}


def test_citation_parsing_and_accuracy():
    cites = M.parse_citations("답입니다 (a.pdf p.3) 그리고 (b.pdf p.5)", [{"file": "a.pdf", "page": 3}])
    assert cites == [("a.pdf", 3), ("b.pdf", 5)]
    ret = [{"file": "a.pdf", "pages": [3]}]
    m = M.citation_metrics(cites, ret, {("a.pdf", 3)})
    assert m["in_retrieved"] == 0.5 and m["correct"] == 0.5
    assert M.citation_metrics([], ret, {("a.pdf", 3)})["correct"] == 0.0


def test_unsupported_numbers_ignores_page_refs():
    assert M.unsupported_numbers("한도 600만원 (a.pdf p.17)", "연 600만원까지") == []
    assert M.unsupported_numbers("한도 700만원", "연 600만원까지") == ["700"]


@pytest.mark.parametrize("m,want", [
    ({"expected5": "answer", "pred5": "answer", "id_hit5": 0.0, "score": 2}, "retrieval_fail"),
    ({"expected5": "answer", "pred5": "abstain", "id_hit5": 1.0, "score": 0}, "wrong_refusal"),
    ({"expected5": "clarify", "pred5": "answer", "id_hit5": None, "score": 1}, "behavior_mismatch"),
    ({"expected5": "answer", "pred5": "answer", "id_hit5": 1.0, "score": 2, "hallucination": True}, "hallucination"),
    ({"expected5": "answer", "pred5": "answer", "id_hit5": 1.0, "score": 0}, "generation_error"),
    ({"expected5": "answer", "pred5": "answer", "id_hit5": None, "score": 0}, "no_gold"),
    ({"expected5": "answer", "pred5": "answer", "id_hit5": 1.0, "score": 2}, None),
])
def test_failure_classification(m, want):
    assert M.classify_failure({}, m) == want


# ------------------------------------------------------------------ 엔드투엔드 (합성 데이터, 가짜 AI)
def _world(tmp_path, cfg_mode="bm25", pipeline="rag"):
    d = tmp_path / "data"
    gl = [{"chunk_id": "structural__glossary__1", "text": "휴면성 증권계좌\n6개월 이상 매매가 없고 10만원 이하인 계좌", "embed_text": "x",
           "file": "financial_glossary_readable.pdf", "doc_type": "glossary", "page": 1, "pages": [1], "half": None, "section": "금융용어집",
           "content_type": "text", "source": "text_layer", "source_ids": ["fsc-glossary-1"], "parent_id": None,
           "links": {"prev": None, "next": None, "article_refs": [], "figure_refs": []}, "chunk_index": 0}]
    write_jsonl(d / "chunks" / "structural.jsonl", gl)
    write_jsonl(d / "sources.jsonl", [{"source_id": "fsc-glossary-1", "doc_type": "glossary", "file": gl[0]["file"], "pages": [1],
                                       "title": "t", "text": "x"}])
    for n in ("pages", "figures"):
        write_jsonl(d / f"{n}.jsonl", [])
    q = tmp_path / "q"
    a = tmp_path / "a"
    q.mkdir(); a.mkdir()
    (q / "positive_questions.json").write_text(json.dumps([{"case_id": "POS001", "question": "휴면성 증권계좌는 어떤 계좌인가요?"}],
                                                          ensure_ascii=False), encoding="utf-8")
    write_jsonl(a / "positive_cases.jsonl", [{"case_id": "POS001", "category": "grounded_qa", "expected_behavior": "answer_with_citation",
                                              "expected_source_ids": ["fsc-glossary-1"], "must_include": ["6개월 이상", "10만원 이하"],
                                              "must_not_include": [], "reference_answer": "6개월 이상 ... 10만원 이하", "set": "positive"}])
    cfg = {"exp_id": "t", "pipeline": pipeline, "paths": {"data": str(d), "runs": str(tmp_path / "runs"), "questions": str(q),
                                                           "answers": str(a)}, "retrieval": {"mode": cfg_mode}}
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


@pytest.mark.parametrize("pipeline", ["rag", "agent"])
def test_dry_run_writes_run_directory(tmp_path, pipeline):
    from src.evaluation.run import main
    p = _world(tmp_path, pipeline=pipeline)
    main(["--config", str(p), "--sets", "positive", "--dry-run"])
    run = tmp_path / "runs" / "dry_t"
    for f in ("config.json", "results.jsonl", "scores.jsonl", "summary.json"):
        assert (run / f).exists(), f
    res = json.loads((run / "results.jsonl").read_text(encoding="utf-8").splitlines()[0])
    for k in ("case_id", "question", "answer", "behavior", "retrieved", "citations", "usage"):
        assert k in res
    assert res["retrieved"][0]["chunk_id"] == "structural__glossary__1" and res["usage"]["llm_calls"] >= 2
    cfgr = json.loads((run / "config.json").read_text(encoding="utf-8"))
    assert cfgr["diff_vs_baseline"].get("retrieval.mode") and "prompts" in cfgr and cfgr["models"]["judge"] == "gemini-2.5-pro"
    sc = json.loads((run / "scores.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert sc["retrieval"]["id_hit5"] == 1.0 and sc["retrieval"]["page_hit5"] == 1.0 and sc["match5"] is True
    summ = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert summ["overall"]["id_hit5"] == 1.0 and summ["by_set"]["positive"]["n"] == 1
    exps = (tmp_path / "runs" / "experiments.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(exps[-1])["exp_id"] == "dry_t"


def test_run_refuses_existing_dir_and_unknown_label(tmp_path):
    from src.common.config import UnknownBehaviorLabel
    from src.evaluation.run import main
    p = _world(tmp_path)
    main(["--config", str(p), "--sets", "positive", "--dry-run"])
    with pytest.raises(SystemExit):
        main(["--config", str(p), "--sets", "positive", "--dry-run"])         # 같은 exp_id 덮어쓰기 거부
    ans = tmp_path / "a" / "positive_cases.jsonl"
    row = json.loads(ans.read_text(encoding="utf-8"))
    row["expected_behavior"] = "brand_new_label"
    write_jsonl(ans, [row])
    with pytest.raises(UnknownBehaviorLabel):
        main(["--config", str(p), "--sets", "positive", "--dry-run", "--exp-id", "u"])


def test_retrieval_only_needs_no_ai(tmp_path):
    from src.evaluation.run import main
    p = _world(tmp_path)
    main(["--config", str(p), "--sets", "positive", "--retrieval-only", "--exp-id", "r"])
    s = json.loads((tmp_path / "runs" / "r" / "summary.json").read_text(encoding="utf-8"))
    assert s["overall"]["id_hit5"] == 1.0 and s["overall"]["cost"]["llm_calls"] == 0


def test_golden_set_empty_template_ok(cfg):
    assert load_set("golden", cfg) == []                                    # 사람이 쓰기 전에는 비어 있어도 오류 아님
    with pytest.raises(KeyError):
        load_set("nope", cfg)


def test_real_question_sets_load_and_scopes(cfg):
    sets = {s: load_set(s, cfg) for s in ("positive", "negative", "life_guide", "additional")}
    assert [len(v) for v in sets.values()] == [60, 40, 20, 60]
    assert sets["positive"][0]["scope"] == ["glossary", "terms"] and sets["additional"][0]["scope"] == ["additional"]
    assert sum(1 for v in sets.values() for c in v if c["expected_source_ids"]) == 148
