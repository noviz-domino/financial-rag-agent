"""노이즈 정리 규칙: cid·특수 글리프, 머리말 반복, 쪽 번호, 펼침면, 위첨자."""
from pathlib import Path

import pdfplumber
import pytest

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
