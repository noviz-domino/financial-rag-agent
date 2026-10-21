"""표: 손실 판정(한글·숫자 각각 90%), 병합 칸 채우기, 행 머리글 반복, 긴 표 나누기."""
from pathlib import Path

import pdfplumber
import pytest

from src.tables.extract import (extract_tables, fill_grid, is_lossy, loss_scores, repeat_row_headers, split_table_md,
                                to_markdown)

ROOT = Path(__file__).resolve().parents[1]


def test_loss_routing_hangul_and_digits_separately():
    raw = "총급여액 5,500만원 이하 초과 공제한도 연금저축 600만원 900만원"
    ok_md = "| 총급여액 | 5,500만원 이하 | 초과 |\n|---|---|---|\n| 공제한도 연금저축 | 600만원 | 900만원 |"
    assert not is_lossy(raw, ok_md)
    no_digits = "| 총급여액 | 이하 | 초과 |\n|---|---|---|\n| 공제한도 연금저축 | 만원 | 만원 |"
    assert is_lossy(raw, no_digits)                               # 한글은 남아도 숫자가 빠지면 손실
    no_hangul = "| 5,500 | 600 | 900 |\n|---|---|---|"
    assert is_lossy(raw, no_hangul)                               # 숫자는 남아도 행 이름이 빠지면 손실
    h, d = loss_scores(raw, no_digits)
    assert h >= 0.9 and d < 0.9


def test_merged_cell_fill_colspan_and_rowspan():
    # 2x3 격자: (0,0)-(1,0) 세로 병합 '한도', (0,1)-(0,2) 가로 병합 '600만원', 나머지 단일 칸
    cells = [(0, 0, 10, 20), (10, 0, 30, 10), (10, 10, 20, 20), (20, 10, 30, 20)]
    texts = {cells[0]: "한도", cells[1]: "600만원", cells[2]: "이하", cells[3]: "초과"}
    grid = fill_grid(cells, lambda b: texts[b])
    assert grid[0] == ["한도", "600만원", "600만원"]               # 가로 병합 → 양쪽 칸 모두
    assert grid[1] == ["한도", "이하", "초과"]                      # 세로 병합 → 아래 행에도 반복


def test_slot_text_for_cells_without_geometry():
    cells = [(0, 0, 10, 10), (10, 0, 20, 10), (0, 10, 10, 20)]     # (1,1) 칸은 셀 도형이 없음(병합 칸의 나머지)
    texts = {cells[0]: "a", cells[1]: "b", cells[2]: "c"}
    grid = fill_grid(cells, lambda b: texts[b], slot_text=lambda b: "600만원")
    assert grid[1][1] == "600만원"


def test_row_header_repeat():
    rows = [["구분", "값"], ["공제", "1"], ["", "2"], ["", "3"], ["합계", "6"]]
    out = repeat_row_headers(rows)
    assert [r[0] for r in out] == ["구분", "공제", "공제", "공제", "합계"]


def test_long_table_split_repeats_header():
    rows = "\n".join(f"| 행{i} | {i * 100} |" for i in range(40))
    md = "| 이름 | 값 |\n|---|---|\n" + rows
    parts = split_table_md(md, 200, "캡션")
    assert len(parts) > 1
    for p in parts:
        assert "| 이름 | 값 |" in p and p.startswith("[표] 캡션")      # 어느 조각만 봐도 열 이름이 보임
    assert all(len(p) <= 260 for p in parts)
    got = "".join(parts)
    assert all(f"행{i} " in got for i in range(40))                  # 행 손실 없음


def test_markdown_escapes_pipe():
    assert "\\|" in to_markdown([["a|b", "c"], ["1", "2"]])


@pytest.mark.skipif(not (ROOT / "01_data/financial_life_guide.pdf").exists(), reason="원본 PDF 없음")
def test_real_guide_p17_table_filled():
    from src.loading.loader import split_spread
    with pdfplumber.open(ROOT / "01_data/financial_life_guide.pdf") as pdf:
        _, region = split_spread(pdf.pages[16])[1]
        tabs = extract_tables(region.dedupe_chars())
    filled = [t for t in tabs if t["status"] == "filled"]
    assert filled and "600만원" in filled[0]["markdown"]
    row = [l for l in filled[0]["markdown"].split("\n") if "연금저축" in l][0]
    assert row.count("600만원") == 2        # '5,500만원 이하/초과' 두 칸 모두에 채워짐
