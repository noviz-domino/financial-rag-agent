"""그림 영역 추출 → PNG 크롭 → (OCR) → Gemini Markdown 변환.

그림 후보 3종 (영역 = src/loader.py 와 같은 반쪽 페이지 단위)
  raster       : PDF 안의 비트맵 이미지. 겹치는 이미지는 합쳐 한 영역으로 ('이미지 안의 이미지')
  vector_chart : 선·사각형·곡선으로 그려진 차트/도식. 이미지가 아니라서 '이미지만 자르기'로는 놓침
  lossy_table  : 로더가 셀 추출에서 값이 빠졌다고 판정한 표 → 그림처럼 VLM 으로 다시 읽음

비용 절감 장치 (AI 호출 전에 거름)
  - 면적 2% 미만 제외, 같은 그림(렌더링 해시 동일)은 1번만 변환하고 나머지는 결과 공유
  - 한 문서에 3번 이상 반복되는 그림 = 장식(아이콘·캐릭터)으로 보고 제외
  - 벡터 영역은 안에 글자(숫자 4개 이상 또는 단어 8개 이상)가 있어야 후보

출력
  data/figures/img/*.png, data/figures/figures.jsonl
  각 행: figure_id, file, doc_type, page, half, kind, bbox, image_path, sha, dup_of, decorative,
        caption, text_layer(영역 안 PDF 텍스트 = 벡터 차트 숫자 검증용), ocr_text, md, md_source, num_check

실행
  python -m src.figures                     # 추출만 (비용 0)
  python -m src.figures --ocr winocr                    # + OCR (엔진 이름으로 선택)
  python -m src.figures --ocr winocr --vlm gemini       # + VLM Markdown 변환 (API 키 필요, 비용 발생)
  python -m src.figures --vlm gemini --limit 5          # 5개만 시험 변환
  python -m src.components                              # 엔진별 사용 가능 여부
"""
import argparse
import hashlib
import io
import json
import os
import re
from collections import Counter
from pathlib import Path

import pdfplumber

from src.loader import DATA, doc_type_of, extract_tables, split_spread, vertical_runs

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "figures"
IMG = OUT / "img"
CACHE = ROOT / "data" / "cache" / "vlm_md"
CAPTION = re.compile(r"^\s*(<?\s*그림|\[그림|참고\s*\d|도표|차트|\[표|표\s*[IVXⅠ-Ⅹ\d]|■|※)")
NUM = re.compile(r"-?\d[\d,]*\.?\d*")


# ------------------------------------------------------------------ 영역 계산
def _union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _overlap(a, b, pad=0.0):
    return a[0] - pad < b[2] and b[0] - pad < a[2] and a[1] - pad < b[3] and b[1] - pad < a[3]


def _area(b):
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


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


def _clip(b, r):
    return (max(b[0], r[0]), max(b[1], r[1]), min(b[2], r[2]), min(b[3], r[3]))


def _stream_hash(im):
    try:
        return hashlib.md5(im["stream"].get_rawdata()).hexdigest()[:12]
    except Exception:
        return f"{im.get('name')}-{im.get('srcsize')}"


def raster_regions(region, min_ratio=0.02):
    """[(bbox, 원본 이미지 스트림 해시)] — 같은 배너·아이콘이 페이지마다 반복되는지 판정하는 데 사용."""
    rb = region.bbox
    ims = [(_clip((im["x0"], im["top"], im["x1"], im["bottom"]), rb), _stream_hash(im)) for im in region.images]
    ims = [(b, h) for b, h in ims if _area(b) > 0]
    out = []
    for m in merge_boxes([b for b, _ in ims], pad=2):
        if _area(m) >= min_ratio * _area(rb):
            out.append((m, "+".join(sorted({h for b, h in ims if _overlap(b, m)}))))
    return out


def vector_regions(region, table_boxes=(), min_ratio=0.03, min_objs=15, pad=4):
    """선·사각형·곡선을 묶어 차트/도식 영역을 찾는다.
    표 안의 도형(셀 테두리)은 먼저 빼고 묶는다. 그렇지 않으면 바로 아래 표와 차트가 한 덩어리가 됨."""
    rb = region.bbox
    ra = _area(rb)
    objs = [_clip((o["x0"], o["top"], o["x1"], o["bottom"]), rb) for o in region.curves + region.rects + region.lines]
    objs = [b for b in objs if _area(b) < 0.5 * ra                                   # 페이지 배경 상자 제외
            and not any(_area(_clip(b, t)) >= 0.8 * max(_area(b), 1e-6) or
                        (b[0] >= t[0] - 1 and b[2] <= t[2] + 1 and b[1] >= t[1] - 1 and b[3] <= t[3] + 1)
                        for t in table_boxes)]
    # 1차: 붙어 있는 도형끼리 묶기. 작은 묶음(3개 이상)도 남긴다:
    #   주택연금 p.72 아래쪽처럼 테두리 없이 행마다 상자만 그린 표는 묶음 하나가 도형 3~5개뿐이라
    #   여기서 버리면 표의 절반이 그림 후보에서 빠진다
    pieces = [m for m in merge_boxes(objs, pad=pad) if sum(1 for b in objs if _overlap(b, m)) >= 3]
    # 2차: 가까운 조각(나란한 패널, 위아래로 쌓인 행 상자)을 한 그림으로 합친 뒤,
    #      '합친 뒤의 전체 도형 수'로 그림인지 판단. 축 라벨이 들어오도록 15pt 확장
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
    """그림 영역 바로 옆(좌우 max_gap pt 안)에 세로로 쓴 라벨이 있으면 영역에 포함한다.
    주택연금 p.72 의 '가입 가능 / 가입 불가'처럼 행 묶음의 의미를 정하는 라벨은 글자라서 도형 묶기에 안 잡히고,
    옆의 회색 막대도 상자들과 30pt 넘게 떨어져 있어 빠졌었다. 라벨 세로 범위의 절반 이상이 그림과 겹쳐야 포함."""
    x0, top, x1, bottom = box
    for (lx0, ltop, lx1, lbottom), _ in vlabels:
        v_overlap = min(bottom, lbottom) - max(top, ltop)
        if v_overlap < 0.5 * (lbottom - ltop):
            continue
        # 좌우 max_gap 안에 있거나, 경계에 걸쳐 있거나, 안에 있으면 '옆 라벨'로 본다
        # (예전 72쪽 영역은 x=66 부터였고 라벨은 x 61~69 라 경계에 걸쳐 반쯤 잘렸었다)
        if lx1 >= x0 - max_gap and lx0 <= x1 + max_gap:
            x0, top = min(x0, lx0 - pad), min(top, ltop - pad)
            x1, bottom = max(x1, lx1 + pad), max(bottom, lbottom + pad)
    return _clip((x0, top, x1, bottom), rb)


def _caption_between(words, top, bottom, x0, x1):
    """두 영역 사이(세로 top~bottom, 가로 x0~x1)에 '그림/표/참고…' 캡션 줄이 있는가 → 있으면 서로 다른 그림."""
    if bottom - top < 1:
        return False
    lines = {}
    for w in words:
        if top <= w["top"] and w["bottom"] <= bottom and w["x1"] > x0 and w["x0"] < x1:
            lines.setdefault(round(w["top"]), []).append(w)
    return any(CAPTION.match(" ".join(x["text"] for x in sorted(ws, key=lambda x: x["x0"]))) for ws in lines.values())


def absorb_adjacent_tables(box, table_boxes, pieces, words, gap=20):
    """그림 영역에 맞닿은 표(와 그 옆의 행 상자)를 같은 그림으로 합친다.
    주택연금 p.72: 위쪽(빨간 테두리 = 가입 가능)만 그림으로 잡히고, 테두리 없는 아래쪽(가입 불가)은
    표 영역 안이라 도형이 빠져 그림에서 잘렸음. 사이에 캡션 줄이 있으면 별개의 그림이므로 합치지 않는다."""
    changed = True
    while changed:
        changed = False
        for t in list(table_boxes) + list(pieces):
            if _overlap(t, box) and t[0] >= box[0] - 1 and t[2] <= box[2] + 1 and t[1] >= box[1] - 1 and t[3] <= box[3] + 1:
                continue  # 이미 포함
            x_ov = min(t[2], box[2]) - max(t[0], box[0])
            near = _overlap(t, box, pad=gap)
            if not near or x_ov < 0.5 * (t[2] - t[0]):
                continue
            top, bottom = (box[3], t[1]) if t[1] >= box[3] else (t[3], box[1])
            if _caption_between(words, top, bottom, max(t[0], box[0]), min(t[2], box[2])):
                continue
            box = _union(box, t)
            changed = True
    return box


def grow_up(box, words, rb, gap=14, max_extra=60):
    """차트 바로 위에 줄 간격(gap) 이내로 붙어 있는 글자 줄(범례·제목)을 영역에 포함한다.
    시험 변환에서 범례가 잘려 Gemini 가 선과 계열 이름을 잘못 연결한 문제를 막기 위함.
    본문 문단까지 끌려오지 않도록 최대 max_extra pt 까지만 넓힌다."""
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
    """그림의 제목 줄 찾기. 우선순위:
      ① 그림 영역 안 첫 두 줄이 '그림/표/참고…'로 시작 (제목이 표 테두리 안에 있는 경우)
      ② 위쪽 gap pt 안에서 가장 가까운 '그림/표/참고…' 줄 (사이에 '(단위: …)' 줄이 끼어도 찾도록 70pt)
      ③ 아래쪽은 '자료:/주:' 줄만 — 아래에 있는 '[표 …]'는 다음 표의 제목이라 쓰면 안 됨
      ④ 바로 위 20pt 안의 줄
    가로 범위는 그림 폭 ±20pt 로 제한 (2단 문서에서 옆 단 글자를 캡션으로 오인하지 않게)."""
    rb = region.bbox
    x0, x1 = max(rb[0], box[0] - 20), min(rb[2], box[2] + 20)

    def lines_in(top, bottom):
        if bottom - top <= 1:
            return []
        return [l.strip() for l in (region.within_bbox((x0, top, x1, bottom)).extract_text() or "").split("\n") if l.strip()]

    inside = lines_in(box[1], box[3])[:2]
    for ln in inside:
        if CAPTION.match(ln):
            return ln
    above = lines_in(max(rb[1], box[1] - gap), box[1])
    for ln in reversed(above):
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


# ------------------------------------------------------------------ 추출
CHARS = re.compile(r"[가-힣A-Za-z0-9]")


def hidden_chars(ocr_text, text_layer):
    """OCR 에는 보이는데 PDF 텍스트 레이어에는 없는 글자 수 = 그림 속에 '그림으로' 들어간 글자."""
    layer = Counter(CHARS.findall(text_layer or ""))
    return sum((Counter(CHARS.findall(ocr_text or "")) - layer).values())


def small_image_clusters(region, min_ratio=0.02, min_count=3, pad=40):
    """면적 2% 미만 이미지(화살표·번호 배지 등)가 min_count 개 이상 모인 곳 → 흐름도 후보 영역.
    화살표 하나는 정보가 없지만, 여러 개가 상자 사이에 놓이면 '단계 순서'를 나타낸다."""
    rb = region.bbox
    small = [_clip((im["x0"], im["top"], im["x1"], im["bottom"]), rb) for im in region.images]
    small = [b for b in small if 0 < _area(b) < min_ratio * _area(rb)]
    out = []
    for m in merge_boxes(small, pad=pad):
        if sum(1 for b in small if _overlap(b, m)) >= min_count:
            out.append(_clip((m[0] - 20, m[1] - 20, m[2] + 20, m[3] + 20), rb))
    return out


def extract_figures(path: Path, router_ocr=None, min_ocr_chars=10):
    """router_ocr: png bytes → 텍스트. 주어지면 OCR 로 '그림 속 글자'를 확인해 후보 판정에 사용(OCR 라우터)."""
    figs = []
    with pdfplumber.open(path) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            for half, region in split_spread(page):
                # 겹쳐 찍은 굵은 글자('3.13%.1%' → '3.1%')를 복원해야 숫자 검증의 기준 텍스트가 깨지지 않음
                region = region.dedupe_chars()
                rb = region.bbox
                tables = extract_tables(region)
                table_boxes = [t["bbox"] for t in tables]
                cands = []   # (kind, bbox, stream_hash, route, png, ocr_text)

                def add(kind, b, route, stream=None, png=None, otext=None):
                    cands.append((kind, b, stream, route, png, otext))

                for b, h in raster_regions(region):
                    add("raster", b, "raster", h)
                vlabels = [(vb, t) for vb, t in vertical_runs(region) if re.search(r"[가-힣A-Za-z]", t)]
                for b in vector_regions(region, table_boxes):
                    b = attach_side_labels(b, vlabels, rb)
                    if any(_area(_clip(b, c[1])) > 0.5 * _area(b) for c in cands):
                        continue  # 이미 raster 후보에 포함
                    ws = words_in(region, b)
                    nums = sum(1 for w in ws if NUM.fullmatch(w["text"].strip("()%")))
                    if nums >= 4 or len(ws) >= 8:
                        add("vector_chart", b, "text_layer")
                    elif router_ocr:
                        # 텍스트 레이어에 글자가 거의 없어도, 글자를 도형으로 바꾼 차트일 수 있음 → OCR 로 확인
                        box = _clip((b[0] - 4, b[1] - 4, b[2] + 4, b[3] + 4), rb)
                        png = render(page, box)
                        otext = router_ocr(png)
                        if len(CHARS.findall(otext)) >= min_ocr_chars:
                            add("vector_chart", b, "ocr_text", png=png, otext=otext)
                for b in small_image_clusters(region):
                    if not any(_area(_clip(b, c[1])) > 0.5 * _area(b) for c in cands):
                        add("diagram", b, "small_image_cluster")
                for t in tables:
                    if t["lossy"]:
                        add("lossy_table", t["bbox"], "lossy_table")

                for i, (kind, b, stream, route, png, otext) in enumerate(cands):
                    box = _clip((b[0] - 4, b[1] - 4, b[2] + 4, b[3] + 4), rb)
                    png = png or render(page, box)
                    text_layer = " ".join(w["text"] for w in words_in(region, box))
                    if router_ocr and otext is None:
                        otext = router_ocr(png)
                    figs.append({
                        "figure_id": f"{path.stem}__p{page_no}{half or ''}__f{i}",
                        "file": path.name, "doc_type": doc_type_of(path), "page": page_no, "half": half,
                        "kind": kind, "route": route, "bbox": [round(v, 1) for v in box],
                        "area_ratio": round(_area(box) / _area(rb), 3),
                        "sha": hashlib.sha256(png).hexdigest()[:16], "_png": png,
                        "stream_hash": stream, "n_words": len(text_layer.split()),
                        "caption": caption_of(region, box), "text_layer": text_layer,
                        "ocr_text": otext, "ocr_chars": len(CHARS.findall(otext or "")) if otext is not None else None,
                        "hidden_chars": hidden_chars(otext, text_layer) if otext is not None else None,
                        "ocr_engine": None, "md": None, "md_source": None, "num_check": None,
                    })
    # 중복: 렌더링 결과가 완전히 같으면 1번만 변환
    first = {}
    for f in figs:
        f["dup_of"] = first.setdefault(f["sha"], f["figure_id"])
        f["dup_of"] = None if f["dup_of"] == f["figure_id"] else f["dup_of"]
    # 제외 규칙 (OCR 결과가 있으면 '그림 속 글자'가 있는 것은 살린다)
    cnt = Counter(f["stream_hash"] for f in figs if f["kind"] == "raster")
    for f in figs:
        f["skip_reason"] = None
        hid = f["hidden_chars"]
        if f["kind"] == "raster" and cnt[f["stream_hash"]] >= 3 and (hid is None or hid < 15):
            f["skip_reason"] = "repeated_image"          # 반복 배너·아이콘 (그림 속 글자가 없을 때만)
        elif f["kind"] == "raster" and f["area_ratio"] >= 0.4 and f["n_words"] >= 30 and (hid is None or hid < 30):
            f["skip_reason"] = "text_over_background"    # 글자가 얹힌 배경 (정보는 텍스트 레이어에)
        elif f["kind"] == "raster" and f["area_ratio"] < 0.05 and f["ocr_chars"] is not None and f["ocr_chars"] < 15:
            f["skip_reason"] = "small_no_text"           # QR 코드·로고·아이콘
        f["decorative"] = f["skip_reason"] is not None
    return figs


# ------------------------------------------------------------------ OCR (옵션, src/components/ocr.py)
def ocr_engine(name, scale=2.0):
    """레지스트리에서 OCR 엔진 생성. 반환: png bytes → 텍스트. scale: 작은 글씨 인식을 위한 확대 배율."""
    import numpy as np
    from PIL import Image

    from src.components import ocr as ocr_reg
    engine = ocr_reg.REGISTRY.create(name)

    def run(png: bytes) -> str:
        im = Image.open(io.BytesIO(png)).convert("RGB")
        if scale != 1:
            im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
        return ocr_reg.to_text(engine(np.array(im)))
    return run


# ------------------------------------------------------------------ Gemini Markdown (옵션)
MD_PROMPT = """이 이미지는 한국어 금융 문서에서 잘라낸 그림·차트·표 영역입니다. 검색용 Markdown으로 변환하세요.

규칙
1. 첫 줄: `유형: 막대그래프 | 선그래프 | 원그래프 | 표 | 도식 | 화면캡처 | 사진 | 장식` 중 하나
2. 제목·단위·기준시점·자료 출처가 보이면 그대로 적기
3. 표·막대그래프처럼 값이 숫자로 적혀 있으면 Markdown 표로 (행/열 이름 포함). 읽기 어려우면 `(판독불가)`
4. **숫자는 이미지에 글자로 적혀 있는 것만** 쓴다. 선·막대의 높이나 축 눈금을 보고 값을 읽어 내거나 추정하지 않는다.
   선그래프에서 라벨이 붙은 점(예: 마지막 값)만 숫자로 쓰고, 나머지 구간은 `추세:`로 말로 설명한다
   (예: "2017~2022년 상승 후 2023년 하락"). `(추정)` 값이 들어간 표는 만들지 않는다
5. 범례가 있으면 `범례:`에 계열 이름과 선 색/모양을 먼저 적고, 각 숫자가 어느 계열인지 범례 기준으로 연결한다.
   범례가 잘려 계열을 확신할 수 없으면 계열 이름 대신 `(계열 불명: 주황 선)`처럼 적는다
6. 기관명·상품명·항목명은 이미지에 적힌 글자 그대로 쓴다. 정식 명칭·약칭으로 바꾸거나 보완하지 않는다
   (예: 이미지가 '중소기업은행'이면 'IBK기업은행'으로 쓰지 않음)
7. 도식·화면캡처는 구성 요소와 흐름(→)을 글머리표로
8. 마지막에 `핵심:` 한두 문장 (보이는 내용에서만)
9. 의미 있는 정보가 없는 장식 이미지면 `유형: 장식` 한 줄만 출력
참고용 주변 캡션: {caption}"""


def vlm_md(describe, vlm_name, png: bytes, caption: str):
    """그림 → Markdown (src/components/vlm.py). (이미지 + 프롬프트 + 모델) 해시로 디스크 캐시."""
    CACHE.mkdir(parents=True, exist_ok=True)
    prompt = MD_PROMPT.format(caption=caption or "없음")
    key = hashlib.sha256(png + prompt.encode() + vlm_name.encode()).hexdigest()[:24]
    cache = CACHE / f"{key}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8")) | {"cached": True}
    r = describe(png, prompt)
    out = {"md": r.text, "model": r.model, "tokens_in": r.tokens_in, "tokens_out": r.tokens_out, "seconds": r.seconds}
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out | {"cached": False}


def _digits(n):
    # 쉼표·마침표를 모두 지운 '숫자열'로 비교: OCR 이 228,235 를 228.235 로 읽어도 같은 숫자로 본다
    return re.sub(r"[,.]", "", n).lstrip("-")


def _found(n, ref):
    # 축에는 '17, 19, 21'로, 답에는 '2017년'으로 쓰는 경우: 4자리 연도는 뒤 두 자리가 원문에 있으면 인정
    return n in ref or (re.fullmatch(r"(19|20)\d\d", n) is not None and n[2:] in ref)


def unverified_numbers(md: str, reference: str):
    """VLM 이 쓴 숫자 중 기준 텍스트에 없는 것 (원래 표기 그대로, 중복 제거). 그래프 높이를 읽은 추정값일 가능성."""
    ref = {_digits(n) for n in NUM.findall(reference or "")}
    out = []
    for n in NUM.findall(md or ""):
        if len(_digits(n)) >= 2 and not _found(_digits(n), ref) and n not in out:
            out.append(n)
    return out


def number_check(md: str, reference: str):
    """VLM이 쓴 숫자 중 참조 텍스트(PDF 텍스트 레이어 또는 OCR)에 실제로 있는 비율. 환각 감지용."""
    ref = {_digits(n) for n in NUM.findall(reference or "")}
    got = [_digits(n) for n in NUM.findall(md or "") if len(_digits(n)) >= 2]
    if not got or not ref:
        return None
    return round(sum(1 for n in got if _found(n, ref)) / len(got), 3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="파일명 일부")
    ap.add_argument("--ocr", metavar="ENGINE", default="auto",
                    help="OCR 라우터 엔진: auto(winocr→easyocr 순) | winocr | easyocr | rapidocr | tesseract | none")
    ap.add_argument("--ocr-scale", type=float, default=2.0)
    ap.add_argument("--vlm", metavar="NAME", help="gemini | openai | claude")
    ap.add_argument("--limit", type=int, default=0, help="VLM 변환 개수 제한(시험용)")
    ap.add_argument("--env", default=str(ROOT / ".env"), help="API 키가 든 .env 경로 (기본: 프로젝트 폴더)")
    ap.add_argument("--workers", type=int, default=4, help="VLM 동시 호출 수")
    a = ap.parse_args()
    from dotenv import load_dotenv
    load_dotenv(a.env)

    # 엔진을 먼저 만들어 키 없음·미설치를 1분짜리 추출 전에 바로 알린다 (fail-fast)
    from src.components import ocr as ocr_reg
    ocr_name = ocr_reg.REGISTRY.first_available(["winocr", "easyocr"]) if a.ocr == "auto" else (None if a.ocr == "none" else a.ocr)
    ocr = ocr_engine(ocr_name, a.ocr_scale) if ocr_name else None
    print(f"OCR 라우터: {ocr_name or '사용 안 함'} (x{a.ocr_scale:g})")
    describe = None
    if a.vlm:
        from src.components import vlm as vlm_reg
        describe = vlm_reg.REGISTRY.create(a.vlm)
    IMG.mkdir(parents=True, exist_ok=True)
    figs = []
    for path in sorted(DATA.rglob("*.pdf")):
        if doc_type_of(path) not in ("life_guide", "additional") or (a.only and a.only not in path.name):
            continue
        fs = extract_figures(path, router_ocr=ocr)
        kinds = Counter(f["kind"] for f in fs)
        routes = Counter(f["route"] for f in fs if not f["decorative"])
        todo = sum(1 for f in fs if not f["dup_of"] and not f["decorative"])
        skip = Counter(f["skip_reason"] for f in fs if f["skip_reason"])
        print(f"{path.name[:32]:32s} 후보 {len(fs):3d} {dict(kinds)} 제외 {dict(skip)} → 대상 {todo} {dict(routes)}")
        figs += fs

    usage = Counter()
    by_id = {f["figure_id"]: f for f in figs}
    targets = []
    for f in figs:
        (IMG / f"{f['figure_id']}.png").write_bytes(f["_png"])
        f["image_path"] = f"data/figures/img/{f['figure_id']}.png"
        f["vlm_error"] = None
        if f["dup_of"] or f["decorative"]:
            continue
        if ocr:
            f["ocr_engine"] = f"{ocr_name}@{a.ocr_scale:g}"   # OCR 텍스트는 라우터 단계에서 이미 채워짐
        if describe and (not a.limit or len(targets) < a.limit):
            targets.append(f)

    def convert(f):
        """재시도 포함 1건 변환. 분당 호출 제한(429)·일시 오류는 기다렸다가 다시 시도."""
        import time
        for attempt in range(5):
            try:
                return f, vlm_md(describe, a.vlm, f["_png"], f["caption"]), None
            except Exception as e:  # noqa: BLE001 — SDK 마다 예외 타입이 달라 문자열로 판단
                msg = str(e)
                if attempt < 4 and any(k in msg for k in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500", "timeout")):
                    time.sleep(min(60, 5 * 2 ** attempt))
                    continue
                return f, None, msg[:200]

    if targets:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        print(f"VLM 변환 {len(targets)}개 시작 (동시 {a.workers}개, 캐시 있는 그림은 호출 없음)")
        with ThreadPoolExecutor(max_workers=a.workers) as pool:
            for i, fut in enumerate(as_completed([pool.submit(convert, f) for f in targets]), 1):
                f, r, err = fut.result()
                if err:
                    f["vlm_error"] = err
                    usage["errors"] += 1
                else:
                    f["md"], f["md_source"] = r["md"], f"{a.vlm}:{r['model']}"
                    # 숫자 검증 참조: 벡터 차트·표는 PDF 텍스트 레이어, 비트맵은 OCR, 그리고 캡션
                    ref = f["text_layer"] + "\n" + (f["ocr_text"] or "") + "\n" + (f["caption"] or "")
                    f["num_check"] = number_check(f["md"], ref)
                    f["unverified_numbers"] = unverified_numbers(f["md"], ref)
                    usage.update({"calls": 0 if r["cached"] else 1, "cached": int(r["cached"]),
                                  "tokens_in": (r["tokens_in"] or 0) if not r["cached"] else 0,
                                  "tokens_out": (r["tokens_out"] or 0) if not r["cached"] else 0})
                if i % 20 == 0 or i == len(targets):
                    print(f"  {i}/{len(targets)} {dict(usage)}", flush=True)
    done = sum(1 for f in targets if f.get("md"))
    for f in figs:  # 중복 그림은 원본의 결과를 공유
        if f["dup_of"]:
            src = by_id[f["dup_of"]]
            for k in ("ocr_text", "ocr_engine", "md", "md_source", "num_check", "unverified_numbers", "vlm_error"):
                f[k] = src[k]

    with open(OUT / "figures.jsonl", "w", encoding="utf-8") as fp:
        for f in figs:
            fp.write(json.dumps({k: v for k, v in f.items() if not k.startswith("_")}, ensure_ascii=False) + "\n")
    todo = sum(1 for f in figs if not f["dup_of"] and not f["decorative"])
    print(f"\n그림 후보 {len(figs)}개 → 중복·장식 제외 변환 대상 {todo}개 | VLM 변환 {done}개 {dict(usage)}"
          f" -> {OUT / 'figures.jsonl'}")


if __name__ == "__main__":
    main()
