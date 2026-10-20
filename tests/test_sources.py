"""정답 근거 ID 연결: 출처 목록 기반 (조 번호로 추측하지 않음)."""
import json
from pathlib import Path

import pytest

from src.sources.units import parse_source_list

ROOT = Path(__file__).resolve().parents[1]

SAMPLE = """제1조(목적) ...
출처 목록
계좌 / 제1조(목적) / https://x?seq=121&historySeq=1789
계좌 / 제10조(양도) / https://x?seq=121&historySeq=1789
계좌 / 제10조의2(특례) / https://x?seq=121&historySeq=1789
계좌 / 제11조(해지) / https://x?seq=121&historySeq=1789
"""


def test_source_list_order_not_article_number():
    seq, hseq, order = parse_source_list(SAMPLE)
    assert (seq, hseq) == ("121", "1789")
    # 제11조 의 목록 순번은 4 (조 번호 11 이 아님): 제10조의2 가 한 항목이라 번호가 어긋난다
    assert order == {"제1조": 1, "제10조": 2, "제10조의2": 3, "제11조": 4}


def test_no_source_list_returns_none():
    assert parse_source_list("출처 목록 없음 본문만") is None


@pytest.mark.skipif(not (ROOT / "01_data/standard_terms").exists(), reason="원본 PDF 없음")
def test_real_terms_ids_cover_expected_answers():
    """04_신용거래약관: 기준 답안 expected_source_ids 가 전부 연결되고, 청구 내용이 맞는 조항이다."""
    import pdfplumber
    from src.sources.units import terms_units
    path = next((ROOT / "01_data/standard_terms").glob("04_*.pdf"))
    with pdfplumber.open(path) as pdf:
        units, warn = terms_units(path, pdf)
    ids = {u["source_id"] for u in units}
    seq = next(iter(ids)).split("-")[2]
    exp = set()
    for l in (ROOT / "03_answers/positive_cases.jsonl").read_text(encoding="utf-8").splitlines():
        d = json.loads(l)
        exp |= {i for i in d["expected_source_ids"] if i.startswith(f"kofia-terms-{seq}-")}
    assert exp and exp <= ids, exp - ids
    assert all(u["text"].startswith("제") for u in units)       # 단위 본문은 조 제목으로 시작
