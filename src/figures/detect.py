"""그림 후보 찾기·거르기 (비용 0). 출처: noviz figures.py.

후보 4종 (영역 = 반쪽 페이지 좌표)
  raster        PDF 안의 비트맵 이미지 (겹치는 이미지는 합침)
  vector_chart  선·사각형·곡선으로 그린 차트/도식 (이미지만 자르면 놓치는 것)
  diagram       작은 이미지(화살표) 3개 이상이 모인 흐름도
  lossy_table   표 단계에서 손실 판정이 난 표 (Gemini 이미지 변환 대상)
거르기: 면적 2% 미만 · 같은 이미지 3번 이상 반복(+OCR 새 글자 없음) · 글자가 얹힌 큰 배경 · 작고 글자 없음(QR·로고)
"""
from __future__ import annotations

import hashlib
import io
import re
from collections import Counter
from pathlib import Path

import pdfplumber

from src.loading.loader import doc_type_of, split_spread, vertical_runs

CAPTION = re.compile(r"^\s*(<?\s*그림|\[그림|참고\s*\d|도표|차트|\[표|표\s*[IVXⅠ-Ⅹ\d]|■|※)")
NUM = re.compile(r"-?\d[\d,]*\.?\d*")
CHARS = re.compile(r"[가-힣A-Za-z0-9]")


def _union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _overlap(a, b, pad=0.0):
    return a[0] - pad < b[2] and b[0] - pad < a[2] and a[1] - pad < b[3] and b[1] - pad < a[3]


def _area(b):
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def _clip(b, r):
    return (max(b[0], r[0]), max(b[1], r[1]), min(b[2], r[2]), min(b[3], r[3]))


def merge_boxes(boxes, pad=6.0):
    """pad 만큼 띄워도 닿는 상자들을 반복적으로 합친다."""
    boxes = list(boxes)
    changed = True
    while changed:
        changed, out = False, []
        for b in boxes:
            for i, o in enumerate(out):
                if _overlap(b, o, pad):
                    out[i] = _union(b, o)
                    changed = True
                    break
            else:
                out.append(b)
        boxes = out
    return boxes


def _stream_hash(im):
    try:
        return hashlib.md5(im["stream"].get_rawdata()).hexdigest()[:12]
    except Exception:  # noqa: BLE001
        return f"{im.get('name')}-{im.get('srcsize')}"


def raster_regions(region, min_ratio=0.02):
    rb = region.bbox
    ims = [(_clip((im["x0"], im["top"], im["x1"], im["bottom"]), rb), _stream_hash(im)) for im in region.images]
    ims = [(b, h) for b, h in ims if _area(b) > 0]
    out = []
    for m in merge_boxes([b for b, _ in ims], pad=2):
        if _area(m) >= min_ratio * _area(rb):
            out.append((m, "+".join(sorted({h for b, h in ims if _overlap(b, m)}))))
    return out


def vector_regions(region, table_boxes=(), min_ratio=0.03, min_objs=15, pad=4):
    """선·사각형·곡선을 묶어 차트/도식 영역을 찾는다. 표 안의 도형(셀 테두리)은 먼저 뺀다."""
    rb = region.bbox
    ra = _area(rb)
    objs = [_clip((o["x0"], o["top"], o["x1"], o["bottom"]), rb) for o in region.curves + region.rects + region.lines]
    objs = [b for b in objs if _area(b) < 0.5 * ra
            and not any(_area(_clip(b, t)) >= 0.8 * max(_area(b), 1e-6) or
                        (b[0] >= t[0] - 1 and b[2] <= t[2] + 1 and b[1] >= t[1] - 1 and b[3] <= t[3] + 1)
                        for t in table_boxes)]
    pieces = [m for m in merge_boxes(objs, pad=pad) if sum(1 for b in objs if _overlap(b, m)) >= 3]
    words = region.extract_words()
    out = []
    for m in merge_boxes(pieces, pad=30):
        if sum(1 for b in objs if _overlap(b, m)) < min_objs:
            continue
        m = absorb_adjacent_tables(m, table_boxes, pieces, words)
        m = _clip((m[0] - 15, m[1] - 15, m[2] + 15, m[3] + 15), rb)
        m = grow_up(m, words, rb)
        if min_ratio * ra <= _area(m) < 0.6 * ra:
            out.append(m)
    return out


def attach_side_labels(box, vlabels, rb, max_gap=40, pad=6):
    """그림 영역 바로 옆에 세로로 쓴 라벨(주택연금 p.72 '가입 가능/불가')이 있으면 영역에 포함."""
    x0, top, x1, bottom = box
    for (lx0, ltop, lx1, lbottom), _ in vlabels:
        if min(bottom, lbottom) - max(top, ltop) < 0.5 * (lbottom - ltop):
            continue
        if lx1 >= x0 - max_gap and lx0 <= x1 + max_gap:
            x0, top = min(x0, lx0 - pad), min(top, ltop - pad)
            x1, bottom = max(x1, lx1 + pad), max(bottom, lbottom + pad)
    return _clip((x0, top, x1, bottom), rb)


def _caption_between(words, top, bottom, x0, x1):
    if bottom - top < 1:
        return False
    lines = {}
    for w in words:
        if top <= w["top"] and w["bottom"] <= bottom and w["x1"] > x0 and w["x0"] < x1:
            lines.setdefault(round(w["top"]), []).append(w)
    return any(CAPTION.match(" ".join(x["text"] for x in sorted(ws, key=lambda x: x["x0"]))) for ws in lines.values())


def absorb_adjacent_tables(box, table_boxes, pieces, words, gap=20):
    """그림에 맞닿은 표(와 옆 행 상자)를 같은 그림으로 합친다. 사이에 캡션 줄이 있으면 별개 그림."""
    changed = True
    while changed:
        changed = False
        for t in list(table_boxes) + list(pieces):
            if _overlap(t, box) and t[0] >= box[0] - 1 and t[2] <= box[2] + 1 and t[1] >= box[1] - 1 and t[3] <= box[3] + 1:
                continue
            x_ov = min(t[2], box[2]) - max(t[0], box[0])
            if not _overlap(t, box, pad=gap) or x_ov < 0.5 * (t[2] - t[0]):
                continue
            top, bottom = (box[3], t[1]) if t[1] >= box[3] else (t[3], box[1])
            if _caption_between(words, top, bottom, max(t[0], box[0]), min(t[2], box[2])):
                continue
            box = _union(box, t)
            changed = True
    return box


def grow_up(box, words, rb, gap=14, max_extra=60):
    """차트 바로 위에 붙은 글자 줄(범례·제목)을 영역에 포함 (최대 max_extra pt)."""
    x0, top, x1, bottom = box
    limit = top - max_extra
    while True:
        above = [w for w in words if top - gap <= w["bottom"] <= top + 1 and w["top"] >= limit
                 and w["x1"] > x0 and w["x0"] < x1]
        if not above:
            break
        new_top = min(w["top"] for w in above) - 2
        if new_top >= top:
            break
        top = new_top
    return _clip((x0, top, x1, bottom), rb)


def words_in(region, box):
    return [w for w in region.extract_words() if _overlap((w["x0"], w["top"], w["x1"], w["bottom"]), box)]


def caption_of(region, box, gap=70):
    """그림 제목 줄: ① 영역 안 첫 두 줄 ② 위쪽 gap pt 안의 '그림/표/참고…' 줄 ③ 아래 '자료:/주:' ④ 바로 위 줄."""
    rb = region.bbox
    x0, x1 = max(rb[0], box[0] - 20), min(rb[2], box[2] + 20)

    def lines_in(top, bottom):
        if bottom - top <= 1:
            return []
        return [l.strip() for l in (region.within_bbox((x0, top, x1, bottom)).extract_text() or "").split("\n") if l.strip()]

    for ln in lines_in(box[1], box[3])[:2]:
        if CAPTION.match(ln):
            return ln
    for ln in reversed(lines_in(max(rb[1], box[1] - gap), box[1])):
        if CAPTION.match(ln):
            return ln
    for ln in lines_in(box[3], min(rb[3], box[3] + 36)):
        if ln.startswith(("자료", "주:", "주)")):
            return ln
    near = lines_in(max(rb[1], box[1] - 20), box[1])
    return near[-1] if near else ""


def render(page, box, resolution=150):
    img = page.crop(box).to_image(resolution=resolution).original
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def hidden_chars(ocr_text, text_layer):
    """OCR 에는 보이는데 PDF 텍스트 레이어에는 없는 글자 수 = 그림 속에 '그림으로' 들어간 글자."""
    layer = Counter(CHARS.findall(text_layer or ""))
    return sum((Counter(CHARS.findall(ocr_text or "")) - layer).values())


def small_image_clusters(region, min_ratio=0.02, min_count=3, pad=40):
    rb = region.bbox
    small = [_clip((im["x0"], im["top"], im["x1"], im["bottom"]), rb) for im in region.images]
    small = [b for b in small if 0 < _area(b) < min_ratio * _area(rb)]
    out = []
    for m in merge_boxes(small, pad=pad):
        if sum(1 for b in small if _overlap(b, m)) >= min_count:
            out.append(_clip((m[0] - 20, m[1] - 20, m[2] + 20, m[3] + 20), rb))
    return out


def extract_figures(path: Path, page_records: list[dict], ocr, cfg: dict, min_ocr_chars=10):
    """한 PDF 의 그림 후보 목록. page_records: 같은 파일의 쪽 레코드 (표 bbox·손실 표 정보를 재사용)."""
    fc = cfg.get("figures", {})
    min_ratio, dpi = fc.get("min_area_ratio", 0.02), fc.get("dpi", 150)
    by_region = {(r["page"], r["half"]): r for r in page_records}
    ocr_ok = getattr(ocr, "available", True)   # no-op OCR 이면 '글자 없음'으로 오판해 그림을 버리지 않는다
    figs = []
    with pdfplumber.open(path) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            for half, region in split_spread(page, cfg.get("loading", {}).get("spread_ratio", 1.15)):
                rec = by_region.get((page_no, half))
                if rec is None:
                    continue
                region = region.dedupe_chars()
                rb = region.bbox
                tables = rec["tables"]
                table_boxes = [tuple(t["bbox"]) for t in tables]
                cands = []   # (kind, bbox, stream_hash, route, png, ocr_text)
                for b, h in raster_regions(region, min_ratio):
                    cands.append(("raster", b, h, "raster", None, None))
                vlabels = [(vb, t) for vb, t in vertical_runs(region) if re.search(r"[가-힣A-Za-z]", t)]
                for b in vector_regions(region, table_boxes):
                    b = attach_side_labels(b, vlabels, rb)
                    if any(_area(_clip(b, c[1])) > 0.5 * _area(b) for c in cands):
                        continue
                    ws = words_in(region, b)
                    nums = sum(1 for w in ws if NUM.fullmatch(w["text"].strip("()%")))
                    if nums >= 4 or len(ws) >= 8:
                        cands.append(("vector_chart", b, None, "text_layer", None, None))
                    else:   # 텍스트 레이어에 글자가 거의 없어도 글자를 도형으로 바꾼 차트일 수 있음 → OCR 로 확인
                        box = _clip((b[0] - 4, b[1] - 4, b[2] + 4, b[3] + 4), rb)
                        png = render(page, box, dpi)
                        otext = ocr(png)
                        if len(CHARS.findall(otext)) >= min_ocr_chars:
                            cands.append(("vector_chart", b, None, "ocr_text", png, otext))
                for b in small_image_clusters(region, min_ratio):
                    if not any(_area(_clip(b, c[1])) > 0.5 * _area(b) for c in cands):
                        cands.append(("diagram", b, None, "small_image_cluster", None, None))
                for t in tables:
                    if t["lossy"]:
                        cands.append(("lossy_table", tuple(t["bbox"]), None, "lossy_table", None, None))
                for i, (kind, b, stream, route, png, otext) in enumerate(cands):
                    box = _clip((b[0] - 4, b[1] - 4, b[2] + 4, b[3] + 4), rb)
                    png = png or render(page, box, dpi)
                    text_layer = " ".join(w["text"] for w in words_in(region, box))
                    if otext is None:
                        otext = ocr(png)
                    figs.append({
                        "figure_id": f"{path.stem}__p{page_no}{half or ''}__f{i}", "file": path.name,
                        "doc_type": doc_type_of(path), "page": page_no, "half": half, "kind": kind, "route": route,
                        "bbox": [round(v, 1) for v in box], "area_ratio": round(_area(box) / _area(rb), 3),
                        "sha": hashlib.sha256(png).hexdigest()[:16], "_png": png, "stream_hash": stream,
                        "n_words": len(text_layer.split()), "caption": caption_of(region, box),
                        "text_layer": text_layer, "ocr_text": otext,
                        "ocr_chars": len(CHARS.findall(otext or "")) if ocr_ok else None,
                        "hidden_chars": hidden_chars(otext, text_layer) if ocr_ok else None})
    first = {}
    for f in figs:   # 렌더링 결과가 같으면 1번만 변환
        d = first.setdefault(f["sha"], f["figure_id"])
        f["dup_of"] = None if d == f["figure_id"] else d
    cnt = Counter(f["stream_hash"] for f in figs if f["kind"] == "raster")
    for f in figs:
        f["skip_reason"] = None
        hid = f["hidden_chars"]
        if f["kind"] == "raster" and cnt[f["stream_hash"]] >= 3 and (hid is None or hid < 15):
            f["skip_reason"] = "repeated_image"
        elif f["kind"] == "raster" and f["area_ratio"] >= 0.4 and f["n_words"] >= 30 and (hid is None or hid < 30):
            f["skip_reason"] = "text_over_background"
        elif f["kind"] == "raster" and f["area_ratio"] < 0.05 and f["ocr_chars"] is not None and f["ocr_chars"] < 15:
            f["skip_reason"] = "small_no_text"
        f["decorative"] = f["skip_reason"] is not None
    return figs
