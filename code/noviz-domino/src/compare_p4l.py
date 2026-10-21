"""pymupdf4llm 로더를 우리 로더(src/loader.py)와 같은 조건에서 비교하기 위한 변환기.

가이드북 + 추가 PDF 를 pymupdf4llm 로 페이지별 Markdown 으로 바꿔서
data/processed/pages_p4l_{mode}.jsonl 로 저장한다 (pages_v2.jsonl 과 같은 필드).
→ src.check_loader / src.chunking --pages 로 '로더만 바꾼' 비교가 가능하다.

모드
  layout : pymupdf 2.x 의 레이아웃 모델(GNN, onnx) — 제목 단계·표·그림 영역 인식
  legacy : 규칙 기반(pymupdf_rag) — 예전 pymupdf4llm 1.x 방식

OCR 은 끈다. 내장 RapidOCR 기본 모델이 중국어라 한글 아이콘·그림을 'J', '言号' 같은 잡음으로 바꾸고,
이 PDF 들은 텍스트 레이어가 있어서 OCR 이 필요 없다.

실행:  python -m src.compare_p4l                       # 두 모드 모두 (레이아웃 모드 약 5~10분)
       python -m src.compare_p4l --mode layout --show financial_life_guide.pdf:10
"""
import argparse
import json
import re
import time
from pathlib import Path

import pymupdf4llm

ROOT = Path(__file__).resolve().parent.parent
P = ROOT / "data" / "processed"
PDFS = [ROOT / "01_data/financial_life_guide.pdf"] + sorted((ROOT / "01_data/additional").glob("*.pdf"))


def clean(md):
    """BM25·청킹에 방해되는 HTML 흔적만 정리 (제목 #, 표 | 는 그대로 둔다)."""
    md = re.sub(r"<!--.*?-->", "", md)
    # 표 칸 안의 <br> 은 공백으로 (줄바꿈으로 바꾸면 Markdown 표 행이 끊어짐), 그 밖은 줄바꿈으로
    md = "\n".join(ln.replace("<br>", " ") if ln.lstrip().startswith("|") else ln.replace("<br>", "\n")
                   for ln in md.split("\n"))
    # 각주 번호 위첨자(숫자·괄호뿐)는 내용째 삭제: 남기면 '16.5²)' → '16.52)' 처럼 값이 오염됨
    # (가이드북 장 제목처럼 위첨자 안에 글자가 있으면 내용은 남김)
    md = re.sub(r"<sup>(.*?)</sup>", lambda m: "" if re.fullmatch(r"[\d\s,()*]*", m.group(1)) else m.group(1), md)
    md = re.sub(r"</?(sup|sub|u|b|i)>", "", md)
    md = "\n".join(ln.replace("**", "") if ln.lstrip().startswith("|") else ln for ln in md.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def convert(path, mode, pages=None):
    pymupdf4llm.use_layout(mode == "layout")
    kw = dict(page_chunks=True, show_progress=False)
    if mode == "layout":
        kw.update(use_ocr=False, header=False, footer=False)
    if pages:
        kw["pages"] = [p - 1 for p in pages]
    return pymupdf4llm.to_markdown(str(path), **kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["layout", "legacy", "both"], default="both")
    ap.add_argument("--show", help="FILE.pdf:PAGE 한 페이지만 출력")
    a = ap.parse_args()
    modes = ["layout", "legacy"] if a.mode == "both" else [a.mode]

    if a.show:
        f, pg = a.show.rsplit(":", 1)
        path = next(p for p in PDFS if p.name == f)
        for m in modes:
            print(f"========== {m}\n{clean(convert(path, m, [int(pg)])[0]['text'])}\n")
        return

    for m in modes:
        out, t0 = [], time.time()
        for path in PDFS:
            t = time.time()
            for i, ch in enumerate(convert(path, m)):
                text = clean(ch["text"])
                out.append({"page": ch["metadata"].get("page_number", i + 1), "half": None, "printed_page": None,
                            "layout": f"p4l_{m}", "tables": [], "figure_lines": [], "figure_summary": None,
                            "needs_vision": False, "image_ratio": None, "text": text, "n_chars": len(text),
                            "file": path.name,
                            "doc_type": "life_guide" if path.name == "financial_life_guide.pdf" else "additional"})
            print(f"  {m:6s} {path.name:45s} {time.time() - t:6.1f}s")
        dst = P / f"pages_p4l_{m}.jsonl"
        with open(dst, "w", encoding="utf-8") as fp:
            for r in out:
                fp.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"-> {dst.name}  {len(out)}쪽  {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
