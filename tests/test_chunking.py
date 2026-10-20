"""청킹: 항 경계 분할, 제목만 있는 조각 금지, 짧은 절 합침, recursive 겹침, 링크."""
from src.chunking.common import recursive_split
from src.chunking.links import add_links, article_refs_in
from src.chunking.structural import GuideChunker, chunk_terms, split_article
from tests.conftest import mk_chunk

HEAD = "제12조(환매일의 조기도래 및 정산)"


def _article(n_clauses=6, width=300):
    marks = "①②③④⑤⑥⑦⑧⑨⑩"
    lines = [HEAD] + [f"{marks[i]} " + ("가나다라" * (width // 4)) + f" 끝{i}" for i in range(n_clauses)]
    return "\n".join(lines)


def test_split_only_at_clause_boundaries_and_title_repeated():
    pieces = split_article(_article(), HEAD, max_chars=700)
    assert len(pieces) > 1
    for i, p in enumerate(pieces):
        first = p.split("\n")[0]
        assert first.startswith(HEAD)                        # 모든 조각이 조 제목으로 시작 (첫 조각은 제목 줄, 나머지는 반복)
        body = p.split("\n")[1:]
        assert body and all(l[0] in "①②③④⑤⑥⑦⑧⑨⑩" for l in body)    # 항 줄 단위 → 문장 중간에서 끊기지 않음
        assert len("\n".join(body)) > 40                      # 제목만 있는 조각 없음
    joined = "\n".join(pieces)
    assert all(f"끝{i}" in joined for i in range(6))         # 내용 손실 없음


def test_short_article_not_split():
    t = f"{HEAD}\n① 짧은 항"
    assert split_article(t, HEAD, 1200) == [t]


def test_chunk_terms_chapter_goes_to_section_path_not_text():
    units = [
        {"source_id": "kofia-terms-1-1-001", "doc_type": "terms", "file": "01_테스트약관.pdf", "pages": [1], "title": "제1조(목적)",
         "article_no": 1, "article_sub": None, "text": "제1조(목적)\n① 이 약관은 목적을 정한다.\n제2장 계좌"},
        {"source_id": "kofia-terms-1-1-002", "doc_type": "terms", "file": "01_테스트약관.pdf", "pages": [1], "title": "제2조(정의)",
         "article_no": 2, "article_sub": None, "text": "제2조(정의)\n① 정의는 다음과 같다. 제1조에 따라"},
    ]
    chunks, parents = chunk_terms(units, "structural", 1200)
    assert len(chunks) == 2 and "제2장" not in chunks[0]["text"]
    assert "제2장 계좌" in chunks[1]["section"] and chunks[1]["source_ids"] == ["kofia-terms-1-1-002"]
    assert chunks[1]["parent_id"] == parents[1]["chunk_id"]
    add_links(chunks)
    assert chunks[1]["links"]["article_refs"][0]["target"] == chunks[0]["chunk_id"]      # '제1조에 따라' → 제1조 청크
    assert chunks[0]["links"]["next"] == chunks[1]["chunk_id"] and chunks[1]["links"]["prev"] == chunks[0]["chunk_id"]


def test_article_refs_skip_own_title_and_external_law():
    text = "제7조(목적)\n① 「자본시장법」 제9조에 따라 제3조의2 및 제5조를 본다."
    refs = article_refs_in(text, 7, None)
    assert (5, None) in refs and (3, 2) in refs and (9, None) not in refs and (7, None) not in refs


def test_guide_short_sections_merged():
    gc = GuideChunker("x.pdf", "additional", "structural", max_chars=1200, short_min=100)
    items = [
        dict(kind="body", text="짧은 절 제목", pages=[3], half=None, section="s", content_type="text", source="text_layer"),
        dict(kind="body", text="본문 " * 80, pages=[3], half=None, section="s", content_type="text", source="text_layer"),
        dict(kind="table", text="[표]\n| a |\n|---|\n| 1 |", pages=[3], half=None, section="s", content_type="table",
             source="pdf_table"),
        dict(kind="body", text="뒤쪽 짧은 글" * 3, pages=[3], half=None, section="s", content_type="text", source="text_layer"),
    ]
    out = gc.merge_short(items)
    assert [c["kind"] for c in out] == ["body", "table", "body"] or len(out) <= 3
    assert all(len(c["text"]) >= 100 for c in out if c["kind"] == "body")       # 100자 미만 본문 조각이 남지 않음
    assert "짧은 절 제목" in out[0]["text"]                                         # 앞 짧은 절은 뒤 청크 앞에 합쳐짐


def test_recursive_split_size_overlap_and_positions():
    text = "\n".join(f"문장{i:02d} 가나다라마바사 아자차카" for i in range(40))
    parts = recursive_split(text, 200, 40)
    assert len(parts) > 3
    for piece, start in parts:
        assert text[start:start + len(piece)] == piece        # 원문 위치와 일치 → 쪽·근거 ID 연결 가능
        assert len(piece) <= 200
    assert any(parts[i][1] < parts[i - 1][1] + len(parts[i - 1][0]) for i in range(1, len(parts)))   # 겹침 존재


def test_figure_refs_link_text_to_figure_chunk():
    fig = mk_chunk("f1", "a.pdf", "additional", "[그림] 그림 Ⅲ-9 가계부채\n값", content_type="figure", caption="그림 Ⅲ-9 가계부채")
    txt = mk_chunk("t1", "a.pdf", "additional", "자세한 내용은 그림 Ⅲ-9 참고")
    add_links([txt, fig])
    assert txt["links"]["figure_refs"] and txt["links"]["figure_refs"][0]["target"] == "f1"
