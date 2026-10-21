"""OCR 엔진 비교: 사용 가능한 엔진(EasyOCR, Windows OCR, RapidOCR, Tesseract) × 확대 배율.

대상: data/figures/figures.jsonl 에서 장식·중복을 뺀 그림
평가
  - 자동 (vector_chart, lossy_table): 그림 영역의 PDF 텍스트 레이어 = 정답지
        char_recall    : 정답 글자(한글·영문·숫자) 중 OCR 이 읽은 비율
        char_precision : OCR 글자 중 정답에 있는 비율 (헛것을 읽은 정도)
        num_recall     : 정답 숫자 토큰 중 OCR 이 정확히 읽은 비율 (금융 문서에서 가장 중요)
  - 사람 (raster)     : 정답지가 없으므로 compare.html 에서 이미지와 나란히 보고 판단
  - 속도: 그림 1개당 처리 시간(초)

출력
  data/figures/ocr/{engine}.jsonl   그림별 OCR 결과·시간·점수
  data/figures/ocr/summary.json     엔진별 요약
  data/figures/ocr/compare.html     이미지 + 두 엔진 결과 나란히 (사람 평가용)

실행
  python -m src.ocr_compare --limit 10            # 시험
  python -m src.ocr_compare                       # 전체 (EasyOCR 은 CPU 에서 수 분~수십 분)
  python -m src.ocr_compare --engines winocr --scales 1 2 3   # 엔진·배율 지정
  python -m src.components                        # 엔진별 사용 가능 여부
"""
import argparse
import html
import io
import json
import re
import time
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
FIG = ROOT / "data" / "figures"
OUT = FIG / "ocr"
CHARS = re.compile(r"[가-힣A-Za-z0-9]")
NUMS = re.compile(r"\d[\d,]*\.?\d*")


# ------------------------------------------------------------------ 엔진 (src/components/ocr.py 레지스트리)
from src.components import ocr as ocr_reg  # noqa: E402


def make_variant(spec):
    """'winocr@2' → (winocr 엔진, 2배 확대). 확대는 모든 엔진에 같은 방식(LANCZOS)으로 적용해 공정하게 비교."""
    name, _, scale = spec.partition("@")
    scale = float(scale or 1)
    engine = ocr_reg.REGISTRY.create(name)

    def run(img: np.ndarray) -> str:
        if scale != 1:
            im = Image.fromarray(img)
            img = np.array(im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS))
        return ocr_reg.to_text(engine(img))
    return run


# ------------------------------------------------------------------ 점수
def scores(ocr: str, ref: str):
    if not ref or not CHARS.search(ref):
        return {}
    r, o = Counter(CHARS.findall(ref)), Counter(CHARS.findall(ocr))
    inter = sum((r & o).values())
    rn = [n.replace(",", "") for n in NUMS.findall(ref)]
    on = Counter(n.replace(",", "") for n in NUMS.findall(ocr))
    num_hit = sum(min(c, on[n]) for n, c in Counter(rn).items())
    return {
        "char_recall": round(inter / max(sum(r.values()), 1), 3),
        "char_precision": round(inter / max(sum(o.values()), 1), 3),
        "num_recall": round(num_hit / len(rn), 3) if rn else None,
    }


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


# ------------------------------------------------------------------ 실행
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engines", nargs="+", default=None, help="기본: 사용 가능한 엔진 전부")
    ap.add_argument("--scales", nargs="+", default=["1", "2"], help="이미지 확대 배율들 (엔진 × 배율 조합 실행)")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    figs = [json.loads(l) for l in open(FIG / "figures.jsonl", encoding="utf-8")]
    figs = [f for f in figs if not f["decorative"] and not f["dup_of"]]
    if a.limit:
        # 종류별로 고르게 뽑아 시험
        by = {}
        for f in figs:
            by.setdefault(f["kind"], []).append(f)
        figs = [f for i in range(a.limit) for fs in by.values() if i < len(fs) for f in [fs[i]]][:a.limit * len(by)]
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"대상 그림 {len(figs)}개: {dict(Counter(f['kind'] for f in figs))}")

    print(ocr_reg.REGISTRY.report())
    engines = a.engines or [n for n, (ok, _) in ocr_reg.REGISTRY.available().items() if ok]
    variants = [f"{e}@{sc}" for e in engines for sc in a.scales]
    results, summary = {}, {}
    for name in variants:
        t0 = time.perf_counter()
        run = make_variant(name)
        load_sec = round(time.perf_counter() - t0, 1)
        rows = []
        for i, f in enumerate(figs, 1):
            img = np.array(Image.open(ROOT / f["image_path"]).convert("RGB"))
            t = time.perf_counter()
            try:
                text, err = run(img), None
            except Exception as e:  # 한 장 실패가 전체를 멈추지 않게
                text, err = "", repr(e)
            sec = round(time.perf_counter() - t, 2)
            ref = f["text_layer"] if f["kind"] != "raster" else ""
            rows.append({"figure_id": f["figure_id"], "kind": f["kind"], "seconds": sec,
                         "text": text, "error": err, **scores(text, ref)})
            if i % 25 == 0:
                print(f"  {name}: {i}/{len(figs)}")
        with open(OUT / f"{name.replace('@', '_x')}.jsonl", "w", encoding="utf-8") as fp:
            for r in rows:
                fp.write(json.dumps(r, ensure_ascii=False) + "\n")
        auto = [r for r in rows if r["kind"] != "raster"]
        summary[name] = {
            "n": len(rows), "model_load_sec": load_sec,
            "sec_per_image": mean([r["seconds"] for r in rows]),
            "errors": sum(1 for r in rows if r["error"]),
            "auto_eval_n": len(auto),
            "char_recall": mean([r.get("char_recall") for r in auto]),
            "char_precision": mean([r.get("char_precision") for r in auto]),
            "num_recall": mean([r.get("num_recall") for r in auto]),
        }
        results[name] = {r["figure_id"]: r for r in rows}
        print(f"{name}: {summary[name]}")

    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    write_html(figs, results, summary)
    print(f"\n-> {OUT / 'summary.json'}\n-> {OUT / 'compare.html'}")


def write_html(figs, results, summary):
    names = list(results)
    head = "".join(f"<th>{n}</th>" for n in names)
    summ = "".join(
        f"<tr><td>{n}</td>" + "".join(f"<td>{s.get(k)}</td>" for k in
                                      ("sec_per_image", "char_recall", "char_precision", "num_recall", "errors")) + "</tr>"
        for n, s in summary.items())
    rows = []
    for f in figs:
        cells = []
        for n in names:
            r = results[n].get(f["figure_id"], {})
            sc = " · ".join(f"{k}={r[k]}" for k in ("char_recall", "num_recall") if r.get(k) is not None)
            cells.append(f"<td><div class=m>{r.get('seconds')}s {sc}</div><pre>{html.escape(r.get('text') or '')}</pre></td>")
        ref = f"<pre class=ref>{html.escape(f['text_layer'][:600])}</pre>" if f["kind"] != "raster" else ""
        rows.append(
            f"<tr data-kind='{f['kind']}'><td><b>{f['figure_id']}</b><br><span class=k>{f['kind']}</span>"
            f"<br>{html.escape(f['caption'] or '')}<br><img src='../img/{f['figure_id']}.png' loading=lazy>{ref}</td>"
            + "".join(cells) + "</tr>")
    page = f"""<!doctype html><meta charset=utf-8><title>OCR 비교</title>
<style>body{{font:13px sans-serif;margin:16px}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;
vertical-align:top;padding:6px}}img{{max-width:420px;display:block;margin-top:4px}}pre{{white-space:pre-wrap;margin:0}}
.ref{{background:#f3f3f3;color:#555;margin-top:4px}}.m{{color:#06c;margin-bottom:4px}}.k{{color:#a50}}</style>
<h2>OCR 엔진 비교</h2>
<p>자동 점수는 벡터 차트·lossy 표만 (정답 = PDF 텍스트 레이어, 회색 상자). raster 는 이미지를 보고 판단해 주세요.</p>
<table><tr><th>엔진</th><th>초/장</th><th>글자 재현율</th><th>글자 정밀도</th><th>숫자 재현율</th><th>오류</th></tr>{summ}</table>
<p>필터: <select onchange="for(const r of document.querySelectorAll('tr[data-kind]'))r.style.display=(!this.value||r.dataset.kind==this.value)?'':'none'">
<option value=''>전체</option><option>raster</option><option>vector_chart</option><option>lossy_table</option></select></p>
<table><tr><th>그림</th>{head}</tr>{''.join(rows)}</table>"""
    (OUT / "compare.html").write_text(page, encoding="utf-8")


if __name__ == "__main__":
    main()
