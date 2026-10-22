"""노이즈 정리 규칙: cid·특수 글리프, 머리말 반복, 쪽 번호, 펼침면, 위첨자, 각주 풀이 삽입."""
from pathlib import Path

import pdfplumber
import pytest

from src.loading.footnotes import body_size, find_markers, find_notes, plan_footnotes
from src.loading.loader import (clean_line, drop_superscripts, is_axis_line, norm_line, process_region, remove_repeated,
                                split_spread)

ROOT = Path(__file__).resolve().parents[1]


def test_clean_line_pua_and_cid():
    assert clean_line(" 첫째 항목") == "• 첫째 항목"                 # 줄 맨 앞 특수 글리프 → 불릿
    assert clean_line("가나") == "가·나"                             # 한글 사이 → 가운뎃점
    assert clean_line("값") == "값"                                  # 나머지는 삭제
    assert clean_line("(cid:123) 항목") == "• 항목"
    assert clean_line("본문 (cid:7) 중간") == "본문  중간"


def _rec(lines):
    return {"lines": list(lines)}


def test_remove_repeated_only_edges_and_digits_normalized():
    recs = [_rec([f"{n} 주택연금 백문백답", "본문 A", "자료: 한국은행", "본문 B", f"끝 {n}"]) for n in range(10, 20)]
    rep = remove_repeated(recs, min_ratio=0.3, min_count=3, edge=1)      # 가장자리 1줄만 검사
    assert norm_line("10 주택연금 백문백답") in rep
    assert all("주택연금 백문백답" not in " ".join(r["lines"]) for r in recs)
    assert all("자료: 한국은행" in r["lines"] for r in recs)           # 본문 속 반복 줄은 가장자리가 아니므로 남는다


def test_axis_line():
    assert is_axis_line("10 20 30 40") and not is_axis_line("2023년 매출 증가")


def test_spread_split_ratio():
    class P:                                           # 가로 > 세로 x 1.15 → 반쪽 2개
        def __init__(self, w, h): self.width, self.height = w, h
        def crop(self, b): return b
    assert [h for h, _ in split_spread(P(1200, 800))] == ["L", "R"]
    assert [h for h, _ in split_spread(P(900, 800))] == [None]    # 900 < 800*1.15=920


@pytest.mark.skipif(not (ROOT / "01_data/financial_life_guide.pdf").exists(), reason="원본 PDF 없음")
def test_real_pdf_spread_pages_and_page_numbers():
    with pdfplumber.open(ROOT / "01_data/financial_glossary_readable.pdf") as pdf:
        rec = process_region(3, None, pdf.pages[2])
        assert "\n3\n" not in "\n" + "\n".join(rec["lines"]) + "\n"        # 쪽 번호 '3' 이 본문에 남지 않음
        assert rec["printed_page"] == 3
    with pdfplumber.open(ROOT / "01_data/financial_life_guide.pdf") as pdf:
        parts = split_spread(pdf.pages[9])
        assert [h for h, _ in parts] == ["L", "R"]


def test_superscript_ratio_function_exists():
    assert callable(drop_superscripts)


# ---- 각주 풀이를 본문 번호 자리에 (D38)


def _ch(text, x0, top, size=9.5, raised=0.0):
    """가짜 글자 하나 (한 글자 폭 = size). raised: 아랫선을 위로 올린 정도 (위첨자)."""
    return {"text": text, "x0": x0, "x1": x0 + size * 0.9, "top": top, "bottom": top + size - raised, "size": size}


def _word(s, x0, top, size=9.5, raised=0.0):
    out = []
    for t in s:
        out.append(_ch(t, x0, top, size, raised))
        x0 = out[-1]["x1"]
    return out


def _page():
    """본문 '익스포저4)를' + 쪽 아래 각주 '4) 부동산 관련 대출' + 이어지는 들여쓴 줄 + 들여쓰지 않은 표 제목 + 차트 축 숫자."""
    body = _word("가나다라마바사아자차카타파하" * 3, 96, 100)
    body2 = _word("익스포저", 96, 120)
    mk = _word("4)", body2[-1]["x1"], 120, size=7.0, raised=2.4)
    after = _word("를 늘렸다", mk[-1]["x1"], 120)
    note = _word("4) 부동산 관련 대출", 96, 640, size=7.6)
    cont = _word("과 보증을 포괄한다.", 106, 650, size=7.6)
    caption = _word("표 제목", 96, 661, size=7.6)
    axis = _word("2024", 300, 400, size=5.9)
    return body + body2 + mk + after + note + cont + caption + axis


def test_footnote_body_size_ignores_small_majority():
    chars = _word("본문" * 30, 0, 0, size=9.5) + _word("각주" * 60, 0, 50, size=7.6) + _word("제목", 0, 90, size=14)
    assert body_size(chars) == 9.5                      # 각주 글씨가 더 많아도 본문 크기를 고름, 제목(적음)은 제외


def test_footnote_marker_and_note_found():
    chars = _page()
    b = body_size(chars)
    ms = find_markers(chars, b)
    assert [(m["n"], m["prev_body"]) for m in ms] == [(4, True)]
    notes = find_notes(chars, b, skip=frozenset(id(c) for m in ms for c in m["chars"]))
    assert [t for _, t, _ in notes[4]] == ["부동산 관련 대출과 보증을 포괄한다."]   # 들여쓴 줄만 이어 붙이고 표 제목은 끊음


def test_footnote_plan_inlines_and_drops_note():
    chars = _page()
    replace, drop, n_in, n_un = plan_footnotes(chars)
    assert (n_in, n_un) == (1, 0)
    text = "".join(replace.get(id(c), c["text"]) for c in chars if id(c) not in drop)
    assert "익스포저(부동산 관련 대출과 보증을 포괄한다.)를 늘렸다" in text
    assert "4) 부동산" not in text and "표 제목" in text and "2024" in text      # 풀이 줄은 지우고 차트 숫자는 남김 (F1)


def test_footnote_unmatched_and_non_markers_kept():
    title = _word("평가 및 시사점", 96, 100, size=14) + _word("30)", 96 + 14 * 0.9 * 8, 100, size=10.5, raised=3)
    prod = _word("햇살론", 96, 200) + _word("15", 96 + 9.5 * 0.9 * 3, 200, size=7.0, raised=2.4)   # ')' 없음 → 번호 아님
    cross = _word("익스포저", 96, 300) + _word("47)", 96 + 9.5 * 0.9 * 4, 300, size=7.0, raised=2.4)  # 풀이가 다음 쪽
    notes = _word("30) 본고는 작성", 96, 640, size=7.6)
    chars = _word("가나다라마바사아자차" * 6, 96, 50) + title + prod + cross + notes
    replace, drop, n_in, n_un = plan_footnotes(chars)
    assert n_in == 0 and n_un == 1 and not drop        # 제목 뒤 번호는 건드리지 않음, 쪽 넘어가는 47) 은 남김


@pytest.mark.skipif(not (ROOT / "01_data/additional/housing_policy_finance_2025.pdf").exists(), reason="원본 PDF 없음")
def test_real_pdf_footnote_inlined():
    with pdfplumber.open(ROOT / "01_data/additional/housing_policy_finance_2025.pdf") as pdf:
        rec = process_region(3, None, pdf.pages[2])
    text = "\n".join(rec["lines"])
    assert "익스포저(금융기관 또는 금융시장을 통한 부동산 관련 대출" in text
    assert not any(l.startswith(("2)", "3)", "4)")) for l in rec["lines"])        # 쪽 아래 풀이 줄은 본문에 들어가서 지워짐
    assert rec["footnotes"] == {"inlined": 3, "unmatched": 0}
