"""레이아웃을 고려한 PDF 로더.

PDF 한 페이지를 '읽는 순서가 올바른 영역(region)' 단위 레코드로 바꾼다.

처리 순서 (docs/로딩_전략_논의.md 3장)
  ① 펼침면 분리   : 가로 > 세로 페이지를 좌/우 반으로 나눔            (half = "L" / "R")
  ② 사이드바 제거 : 같은 x에 한 글자씩 세로로 쌓인 단어 제거
  ③ 표 추출       : find_tables() → Markdown, 표 영역은 본문에서 제외
  ④ 단(column) 분리: 가운데 세로 여백(gutter)을 찾아 좌단 → 우단 순서로 읽음
                     제목·각주처럼 전체 폭을 쓰는 줄은 '전체 폭 띠'로 따로 처리
  ⑤ 줄 정리       : 쪽번호·반복 머리말/꼬리말 제거, 차트 축 숫자 줄은 figure_lines 로 분리
  ⑥ (옵션) VLM    : 차트·표가 있는 영역을 이미지로 렌더링해 멀티모달 LLM 요약 → figure_summary
  ⑦ (옵션) --merge-p4l : pymupdf4llm(레이아웃 모델) 결과에서 좋은 점만 가져옴 → pages_v3.jsonl
                     - 제목: pymupdf4llm 이 제목으로 본 줄에 '## ' 표시 (청킹의 단락 경계·제목으로 쓰임)
                     - 표  : 우리 lossy 표를 같은 쪽 pymupdf4llm 표가 원문 한글·숫자 90% 이상 담으면 대체
                     읽는 순서·펼침면·세로 라벨·그림 처리는 그대로 우리 로더 (docs/로더_비교_pymupdf4llm.md)

출력 레코드 (data/processed/pages_v2.jsonl, 한 줄 = 한 영역)
  file, doc_type, page(PDF 페이지, 정답 ID 기준), half, printed_page, layout,
  text(본문), tables[{caption, markdown}], figure_lines[], figure_summary,
  needs_vision, image_ratio, n_chars

실행
  python -m src.loader                         # 전체 PDF
  python -m src.loader --only life_guide       # 파일명 일부로 필터
  python -m src.loader --show financial_life_guide.pdf:10   # 한 페이지 결과 확인(기존 추출과 비교)
  python -m src.loader --vlm                   # 차트 요약까지 (API 키 필요, 비용 발생)
  python -m src.compare_p4l --mode layout && python -m src.loader --merge-p4l   # → pages_v3.jsonl
"""
import argparse
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "01_data"
OUT = ROOT / "data" / "processed"
VLM_CACHE = ROOT / "data" / "cache" / "vlm"

TABLE_SETTINGS = [
    {},                                                              # 선이 있는 표 (기본)
    {"vertical_strategy": "lines", "horizontal_strategy": "text"},   # 세로선만 있는 표 (가이드북)
]
NUM_TOKEN = re.compile(r"^[\(\[]?[-+~]?[\d.,]*\d[\d.,]*[%p]?[\)\]]?$|^\(?[%‰]\)?$|^\((조|억|만)?원\)$|^\(%p?\)$")
HANGUL_WORD = re.compile(r"[가-힣]{2,}")
PAGE_NO = re.compile(r"^[-–—\s]*\d{1,3}[-–—\s]*$")


# ------------------------------------------------------------------ 유틸
def doc_type_of(path: Path) -> str:
    if path.name == "financial_glossary_readable.pdf":
        return "glossary"
    if path.name == "financial_life_guide.pdf":
        return "life_guide"
    if path.parent.name == "standard_terms":
        return "terms"
    return "additional"


def _inside(obj, box, pad=1.0):
    x0, top, x1, bottom = box
    cx, cy = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
    return x0 - pad <= cx <= x1 + pad and top - pad <= cy <= bottom + pad


def _drop_chars_in(region, boxes):
    if not boxes:
        return region
    return region.filter(lambda o: not (o.get("object_type") == "char" and any(_inside(o, b) for b in boxes)))


# ------------------------------------------------------------------ ① 펼침면
def split_spread(page):
    if page.width > page.height * 1.15:
        w = page.width / 2
        return [("L", page.crop((0, 0, w, page.height))), ("R", page.crop((w, 0, page.width, page.height)))]
    return [(None, page)]


# ------------------------------------------------------------------ ② 사이드바
def vertical_runs(region, min_run=4):
    """같은 x0(±2pt)에 좌우가 빈 1~2글자 단어가 세로로 min_run 개 이상 연속 → 세로쓰기.
    반환: [(bbox, 이어 붙인 글자)]. 지우지 않고 가로 한 줄로 바꿔 남기는 이유:
      - 한국은행 PDF 사이드바('금융안정상황')는 매 페이지 반복 → remove_repeated 가 제거
      - 주택연금 p.72 흐름표의 '가입 가능 / 가입 불가'는 의미 있는 라벨 → 남아야 함"""
    all_words = region.extract_words()

    def isolated(w, gap=12):
        # 같은 줄 좌우 gap 안에 다른 단어가 있으면 본문(예: 줄 첫 글자 '다.')이지 세로쓰기가 아님
        return not any(o is not w and abs(o["top"] - w["top"]) < 3 and
                       (0 <= o["x0"] - w["x1"] < gap or 0 <= w["x0"] - o["x1"] < gap) for o in all_words)

    def joined(run):
        out = run[0]["text"]
        for a, b in zip(run, run[1:]):
            out += (" " if b["top"] - a["bottom"] > 6 else "") + b["text"]   # 간격이 크면 단어 경계
        return out

    words = [w for w in all_words if len(w["text"]) <= 2 and isolated(w)]
    cols = {}
    for w in words:
        cols.setdefault(round(w["x0"] / 3), []).append(w)
    runs = []
    for ws in cols.values():
        ws.sort(key=lambda w: w["top"])
        run = [ws[0]]
        for w in ws[1:] + [None]:
            if w is not None and w["top"] - run[-1]["bottom"] < 14:
                run.append(w)
                continue
            if len(run) >= min_run:
                box = (min(r["x0"] for r in run), run[0]["top"], max(r["x1"] for r in run), run[-1]["bottom"])
                runs.append((box, joined(run)))
            run = [w] if w is not None else []
    return sorted(runs, key=lambda r: (r[0][1], r[0][0]))


def sidebar_boxes(region, min_run=4):
    return [b for b, _ in vertical_runs(region, min_run)]


# ------------------------------------------------------------------ ③ 표
def _expand_subrows(row):
    """선 없이 줄바꿈으로만 나뉜 하위 행('자산\\n금융자산\\n부동산')을 여러 행으로 펼친다."""
    parts = [(c or "").split("\n") for c in row]
    n = max(len(p) for p in parts)
    if n > 1 and all(len(p) in (1, n) for p in parts):
        return [[p[i] if len(p) == n else (p[0] if i == 0 else "") for p in parts] for i in range(n)]
    return [[(c or "").replace("\n", " ") for c in row]]


def _clean_rows(rows):
    rows = [sub for r in rows for sub in _expand_subrows(r)]
    rows = [[c.strip() for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    keep = [j for j in range(len(rows[0])) if any(j < len(r) and r[j] for r in rows)]
    return [[r[j] if j < len(r) else "" for j in keep] for r in rows]


def to_markdown(rows):
    head, *body = rows
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def extract_tables(region):
    rw, rh = region.bbox[2] - region.bbox[0], region.bbox[3] - region.bbox[1]
    for settings in TABLE_SETTINGS:
        found = []
        for t in region.find_tables(settings):
            rows = _clean_rows(t.extract())
            if len(rows) < 2 or len(rows[0]) < 2:
                continue
            cells = [c for r in rows for c in r]
            filled = sum(1 for c in cells if c) / len(cells)
            x0, top, x1, bottom = t.bbox
            area = (x1 - x0) * (bottom - top) / (rw * rh)
            if filled < 0.35 or sum(1 for c in cells if c) < 4 or area > 0.7:
                continue
            nonempty = [c for c in cells if c]
            if len(rows[0]) >= 8 and sum(len(c) for c in nonempty) / len(nonempty) < 2:
                continue  # 제목 글자가 한 글자씩 칸에 들어간 가짜 표
            # 캡션: 표 바로 위 40pt 안의 줄 (예: "참고 1-3. ...", "(단위: 만원)")
            above = region.within_bbox((max(region.bbox[0], x0 - 5), max(region.bbox[1], top - 40),
                                        min(region.bbox[2], x1 + 5), top))
            caption = " ".join((above.extract_text() or "").split("\n")[-2:]).strip()
            # 손실 검사: 표 영역 원문의 한글·숫자 중 셀로 들어가지 못한 것이 10% 넘으면 lossy
            # (숫자만 세면 행 이름 '적립금' 같은 한글 라벨 누락을 놓침)
            md = to_markdown(rows)
            raw = region.within_bbox(t.bbox).extract_text() or ""
            lossy = False
            for pat in (r"[가-힣]", r"[0-9]"):   # 한글(행 이름)과 숫자(값)를 따로 검사
                n_raw, n_md = len(re.findall(pat, raw)), len(re.findall(pat, md))
                lossy |= n_raw > 0 and n_md < 0.9 * n_raw
            found.append({"bbox": t.bbox, "caption": caption, "markdown": md, "lossy": lossy})
        if found:
            return found
    return []


# ------------------------------------------------------------------ ④ 단 분리
def group_lines(words, tol=3):
    lines = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(w["top"] - lines[-1][0]["top"]) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    return lines


def crosses(line, gx, min_gap=8):
    """이 줄이 x=gx 에서 '단 사이 여백'으로 끊기지 않으면 True.
    1단 문장의 단어 사이 띄어쓰기(3~4pt)는 여백으로 보지 않도록 min_gap 이상만 인정."""
    if any(w["x0"] < gx < w["x1"] for w in line):
        return True
    lefts = [w["x1"] for w in line if w["x1"] <= gx]
    rights = [w["x0"] for w in line if w["x0"] >= gx]
    return bool(lefts and rights) and min(rights) - max(lefts) < min_gap


def find_gutter(words, x0, x1, steps=48):
    """가운데 38~62% 구간에서 '여백 없이 가로지르는 줄'이 가장 적은 x.
    기준을 단어 수가 아니라 줄 수로 잡는 이유: 1단 페이지는 어느 x든 줄마다 단어 1개 정도만
    걸치므로, 단어 수 대비 비율로 보면 1단도 2단처럼 보이기 때문."""
    if len(words) < 40:
        return None
    lines = group_lines(words)
    cands = [x0 + (0.38 + 0.24 * k / steps) * (x1 - x0) for k in range(steps + 1)]
    gx = min(cands, key=lambda g: sum(crosses(l, g) for l in lines))
    n_cross = sum(crosses(l, gx) for l in lines)
    left = sum(1 for w in words if w["x1"] <= gx)
    right = sum(1 for w in words if w["x0"] >= gx)
    if n_cross < 0.3 * len(lines) and left > 0.2 * len(words) and right > 0.2 * len(words):
        return gx
    return None


def read_in_order(region):
    """2단이면 '전체 폭 띠 / 2단 띠'로 나눠 위에서 아래로, 2단 띠는 좌→우 순서로 읽는다."""
    x0, top, x1, bottom = region.bbox
    words = region.extract_words()
    gx = find_gutter(words, x0, x1)
    if gx is None:
        return region.extract_text() or "", "1col"

    # gutter를 가로지르는 단어의 y 구간 = 전체 폭 띠
    spans = sorted((max(min(w["top"] for w in l) - 1, top), min(max(w["bottom"] for w in l) + 1, bottom))
                   for l in group_lines(words) if crosses(l, gx))
    spans = [(s, e) for s, e in spans if e > s]
    full = []
    for s, e in spans:
        if full and s <= full[-1][1] + 3:
            full[-1][1] = max(full[-1][1], e)
        else:
            full.append([s, e])

    bands, cur = [], top
    for s, e in full:
        if s > cur:
            bands.append(("2col", cur, s))
        bands.append(("full", max(s, top), min(e, bottom)))
        cur = e
    if cur < bottom:
        bands.append(("2col", cur, bottom))

    parts = []
    for kind, s, e in bands:
        if e - s < 1:
            continue
        if kind == "full":
            parts.append(region.within_bbox((x0, s, x1, e)).extract_text() or "")
        else:
            parts.append(region.within_bbox((x0, s, gx, e)).extract_text() or "")
            parts.append(region.within_bbox((gx, s, x1, e)).extract_text() or "")
    return "\n".join(p for p in parts if p.strip()), "2col" if not full else "mixed"


# ------------------------------------------------------------------ ⑤ 줄 정리
def is_print_slug(line):
    """인쇄용 작업 파일 표시 줄. 예: '생애주기1권-20대 사회첫걸음_4교.indd'"""
    return ".indd" in line or "iinndddd" in line


def is_axis_line(line):
    toks = line.split()
    if len(toks) < 2 or HANGUL_WORD.search(line):
        return False
    return sum(1 for t in toks if NUM_TOKEN.match(t)) / len(toks) >= 0.6


def printed_page_of(region):
    x0, top, x1, bottom = region.bbox
    h = bottom - top
    cands = [w for w in region.extract_words()
             if re.fullmatch(r"[0-9]{1,3}", w["text"]) and (w["top"] > bottom - 0.12 * h or w["bottom"] < top + 0.07 * h)]
    if not cands:
        return None
    return int(max(cands, key=lambda w: abs(w["top"] - (top + h / 2)))["text"])


def norm_line(line):
    return re.sub(r"\d+", "#", line.strip())


# ------------------------------------------------------------------ 영역 처리
def drop_superscripts(region, ratio=0.65):
    """각주 번호 같은 위첨자(본문 글자보다 훨씬 작은 숫자·괄호)를 제거. 예: '16.5²)' → '16.5'"""
    sizes = sorted(c["size"] for c in region.chars)
    if not sizes:
        return region
    limit = sizes[len(sizes) // 2] * ratio
    return region.filter(lambda o: not (o.get("object_type") == "char" and o["size"] < limit
                                        and re.match(r"[\d)\]*]", o["text"])))


def attach_vertical_labels(tables, vruns):
    """표 옆의 세로 구분 라벨(예: 주택연금 p.72 '가입가능'/'가입불가')을 표의 '구분' 열로 붙인다.
    표 추출은 행·열은 잡지만 표 바깥에서 행 묶음의 의미를 정하는 라벨은 놓치기 때문.
    조건: 라벨의 세로 범위가 표와 절반 이상 겹치고, 라벨이 표의 오른쪽 밖에 있지 않음."""
    for t in tables:
        x0, top, x1, bottom = t["bbox"]
        labels, covers_all = [], False
        for (rx0, rtop, rx1, rbottom), text in vruns:
            if not re.search(r"[가-힣A-Za-z]", text) or rx0 >= x1:
                continue
            overlap = min(bottom, rbottom) - max(top, rtop)
            if overlap >= 0.5 * (rbottom - rtop) and rx1 <= x1:
                labels.append(text.replace(" ", ""))
                # 라벨이 표 높이의 절반 이상을 덮어야 '표 전체의 구분'으로 본다 (일부 행 묶음 라벨을 전체에 붙이지 않게)
                covers_all = (rbottom - rtop) >= 0.5 * (bottom - top)
        if len(labels) == 1 and covers_all:   # 라벨 하나가 표 전체를 덮음 → 모든 행에 같은 구분값
            # 첫 행도 라벨을 붙인다: 이런 표는 첫 행이 머리글이 아니라 데이터인 경우가 많음(p.72 '기본계획수립')
            lines = t["markdown"].split("\n")
            lines[0] = f"| {labels[0]} " + lines[0]
            lines[1] = "|---" + lines[1]
            lines[2:] = [f"| {labels[0]} " + ln for ln in lines[2:]]
            t["markdown"] = "\n".join(lines)
            t["caption"] = f"{t['caption']} [구분: {labels[0]}]".strip()
            t["group_label"] = labels[0]
        elif labels:           # 여러 라벨이 한 표에 걸침 → 행 단위 대응은 VLM 에 맡기고 캡션에만 표시
            t["caption"] = f"{t['caption']} [구분: {' / '.join(labels)}]".strip()
            t["group_label"] = " / ".join(labels)
    return tables


def process_region(page_no, half, region, p4l=None):
    # 굵게 보이려고 같은 글자를 겹쳐 찍은 경우('1100..55%%' → '10.5%')를 먼저 복원
    region = region.dedupe_chars()
    vruns = vertical_runs(region)
    region = drop_superscripts(_drop_chars_in(region, [b for b, _ in vruns]))
    tables = attach_vertical_labels(extract_tables(region), vruns)
    if p4l:
        tables = replace_lossy_tables(region, tables, p4l["tables"])
    # 깨끗한 표만 본문에서 빼고 Markdown으로 대체. lossy 표는 본문 텍스트를 그대로 두어 내용 손실·중복을 막음
    body = _drop_chars_in(region, [t["bbox"] for t in tables if not t["lossy"]])
    text, layout = read_in_order(body)

    lines, fig = [], []
    for ln in text.split("\n"):
        s = ln.strip()
        if not s or PAGE_NO.match(s) or is_print_slug(s):
            continue
        (fig if is_axis_line(s) else lines).append(s)
    if p4l:
        lines = mark_headings(lines, p4l["headings"])
    lines += [f"[세로] {t}" for _, t in vruns if re.search(r"[가-힣A-Za-z]", t)]   # 숫자만 있는 세로줄(쪽번호)은 제외

    area = (region.bbox[2] - region.bbox[0]) * (region.bbox[3] - region.bbox[1])
    img_area = sum(max(0, min(im["x1"], region.bbox[2]) - max(im["x0"], region.bbox[0])) *
                   max(0, min(im["bottom"], region.bbox[3]) - max(im["top"], region.bbox[1])) for im in region.images)
    image_ratio = round(min(img_area / area, 1.0), 3)
    return {
        "page": page_no,
        "half": half,
        "printed_page": printed_page_of(region),
        "layout": layout,
        "lines": lines,
        # bbox: 이 표를 Gemini 그림 영역이 덮는지 판단해 중복 청크를 없애는 데 사용 (chunking 단계)
        "tables": [{"caption": t["caption"], "markdown": t["markdown"], "lossy": t["lossy"],
                    "bbox": [round(v, 1) for v in t["bbox"]],
                    **({"source": t["source"]} if t.get("source") else {})} for t in tables],
        "figure_lines": fig,
        "figure_summary": None,
        "needs_vision": len(fig) >= 3 or image_ratio >= 0.12 or any(t["lossy"] for t in tables),
        "image_ratio": image_ratio,
        "_bbox": region.bbox,
    }


def remove_repeated(records, min_ratio=0.3, min_count=3, edge=3):
    """여러 영역의 머리/꼬리 위치(앞뒤 edge 줄)에 반복되는 줄(머리말·꼬리말·워터마크)을 제거.
    위치를 제한하는 이유: '자료: 한국은행 ...' 같은 본문 속 반복 줄까지 지우지 않기 위해."""
    cnt = Counter()
    for r in records:
        cnt.update({norm_line(l) for l in r["lines"][:edge] + r["lines"][-edge:]})
    limit = max(min_count, min_ratio * len(records))
    repeated = {k for k, v in cnt.items() if v >= limit}
    for r in records:
        r["lines"] = [l for l in r["lines"] if norm_line(l) not in repeated]
    return repeated


def load_pdf(path: Path, p4l=None):
    records = []
    with pdfplumber.open(path) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            for half, region in split_spread(page):
                records.append(process_region(page_no, half, region, (p4l or {}).get((path.name, page_no))))
    repeated = remove_repeated(records) if len(records) >= 3 else set()
    dt = doc_type_of(path)
    for r in records:
        r["text"] = "\n".join(r.pop("lines"))
        r["n_chars"] = len(r["text"])
        r.update(file=path.name, doc_type=dt)
    return records, repeated


# ------------------------------------------------------------------ ⑦ (옵션) pymupdf4llm 합치기
P4L_PAGES = OUT / "pages_p4l_layout.jsonl"


def _hnorm(s):
    return re.sub(r"[^가-힣A-Za-z0-9]", "", re.sub(r"\*\*|__|`", "", s))


def load_p4l():
    """src.compare_p4l 결과 → {(file, page): {"headings": {정규화 제목: 단계}, "tables": [markdown, ...]}}"""
    if not P4L_PAGES.exists():
        raise SystemExit(f"{P4L_PAGES.name} 가 없어요. 먼저: python -m src.compare_p4l --mode layout")
    out = {}
    for line in open(P4L_PAGES, encoding="utf-8"):
        r = json.loads(line)
        heads, tables, cur = {}, [], []
        for ln in r["text"].split("\n") + [""]:
            s = ln.strip()
            m = re.match(r"^(#{1,6})\s+(.*)", s)
            if m and re.search(r"[가-힣]", m.group(2)) and len(_hnorm(m.group(2))) >= 2:
                heads.setdefault(_hnorm(m.group(2)), len(m.group(1)))
            if s.startswith("|"):
                cur.append(s)
            elif cur:
                tables.append("\n".join(cur))
                cur = []
        out[(r["file"], r["page"])] = {"headings": heads, "tables": tables}
    return out


def mark_headings(lines, heads):
    """pymupdf4llm 이 제목(#)으로 본 줄 앞에 '## ' 를 붙인다. 두 줄로 나뉜 제목은 한 줄로 합친다.
    단계(#, ##, ###…)는 pymupdf4llm 이 문서마다 다르게 매겨서 쓰지 않고 '제목이다'만 가져온다."""
    if not heads:
        return lines
    keys = set(heads) | {re.sub(r"^\d+", "", k) for k in heads}   # '2재무설계과정' ↔ '재무설계과정'(장 번호가 따로 찍힌 경우)
    out, i = [], 0
    while i < len(lines):
        a = _hnorm(lines[i])
        if len(a) >= 2 and a in keys:
            out.append("## " + lines[i])
            i += 1
        elif i + 1 < len(lines) and len(a) >= 2 and a + _hnorm(lines[i + 1]) in keys:
            out.append("## " + lines[i] + " " + lines[i + 1])
            i += 2
        else:
            out.append(lines[i])
            i += 1
    return out


def _cover(raw, cand, pat):
    """raw 의 pat 글자 중 cand 에 (개수까지) 들어 있는 비율."""
    a, b = Counter(re.findall(pat, raw)), Counter(re.findall(pat, cand))
    n = sum(a.values())
    return sum(min(v, b[k]) for k, v in a.items()) / n if n else 1.0


NUM_TOK = r"\d[\d,.]*\d|\d"


def replace_lossy_tables(region, tables, p4l_tables):
    """lossy 표 → 같은 쪽의 pymupdf4llm 표가 아래를 모두 만족하면 대체한다.
      - 한글 글자 재현 90% 이상 (행·열 이름이 빠지지 않음)
      - 숫자 '토큰' 재현·정밀 각각 95% 이상 (글자 단위로 세면 '247,738' → '24 | 7,738' 처럼 칸이 잘못 나뉜 값을 놓침)
      - 표 글자의 80% 이상이 이 표 원문에서 옴 (옆 표·본문을 끌어오지 않음)
    세로 라벨이 붙은 표(group_label)는 우리 쪽 구분 정보가 더 중요해서 대체하지 않는다."""
    for t in tables:
        if not t["lossy"] or t.get("group_label"):
            continue
        raw = region.within_bbox(t["bbox"]).extract_text() or ""
        best, score = None, 0
        for md in p4l_tables:
            c = (_cover(raw, md, r"[가-힣]"), _cover(raw, md, NUM_TOK), _cover(md, raw, NUM_TOK),
                 _cover(md, raw, r"[가-힣0-9]"))
            if c[0] >= 0.9 and c[1] >= 0.95 and c[2] >= 0.95 and c[3] >= 0.8 and sum(c) > score:
                best, score = md, sum(c)
        if best:
            t.update(markdown=best, lossy=False, source="pymupdf4llm")
    return tables


def render_text(rec, with_tables=True, with_figures=True, with_lossy_tables=False):
    """청킹이 바로 쓸 수 있는 '본문 + 표 + 차트 요약' 문자열.
    lossy 표는 내용이 이미 본문에 있으므로 기본적으로 Markdown을 붙이지 않는다(중복 방지)."""
    parts = [rec["text"]]
    if with_tables:
        for t in rec["tables"]:
            if t["lossy"] and not with_lossy_tables:
                continue
            parts.append(f"[표] {t['caption']}\n{t['markdown']}".strip())
    if with_figures and rec.get("figure_summary"):
        parts.append(f"[그림 설명] {rec['figure_summary']}")
    elif with_figures and rec.get("figure_lines"):
        # 차트 축 숫자와 선 없는 표의 숫자 행은 줄만 보고는 구분이 안 되므로 버리지 않고 페이지 끝에 모아 둔다
        parts.append("[그림·표 수치] " + " / ".join(rec["figure_lines"]))
    return "\n\n".join(p for p in parts if p)


# ------------------------------------------------------------------ ⑥ VLM (옵션)
VLM_PROMPT = (
    "이 이미지는 금융 문서의 한 페이지입니다. 페이지 안의 차트·그래프·표·도식만 찾아 한국어로 설명하세요.\n"
    "- 각 그림마다: 제목, 단위, 축/범례, 주요 수치(이미지에 보이는 값 그대로), 추세나 비교 결과\n"
    "- 본문 문단, 캐릭터·장식 그림, 로고는 무시하세요\n"
    "- 이미지에 없는 숫자를 추측하지 마세요\n"
    "- 설명할 차트·표·도식이 없으면 '없음'만 출력하세요"
)


def describe_image(png: bytes) -> str:
    """환경 변수에 있는 키로 멀티모달 모델 호출. 결과는 이미지 해시로 디스크 캐시."""
    import base64
    VLM_CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(png + VLM_PROMPT.encode()).hexdigest()[:24]
    cache = VLM_CACHE / f"{key}.txt"
    if cache.exists():
        return cache.read_text(encoding="utf-8")

    b64 = base64.b64encode(png).decode()
    if os.getenv("OPENAI_API_KEY"):
        from openai import OpenAI
        resp = OpenAI().chat.completions.create(
            model=os.getenv("VLM_MODEL", "gpt-4.1-mini"), temperature=0,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": VLM_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}])
        out = resp.choices[0].message.content.strip()
    elif os.getenv("GOOGLE_API_KEY"):
        from langchain_core.messages import HumanMessage
        from langchain_google_genai import ChatGoogleGenerativeAI
        llm = ChatGoogleGenerativeAI(model=os.getenv("VLM_MODEL", "gemini-2.5-flash"), temperature=0)
        out = llm.invoke([HumanMessage(content=[
            {"type": "text", "text": VLM_PROMPT},
            {"type": "image_url", "image_url": f"data:image/png;base64,{b64}"}])]).content.strip()
    else:
        raise RuntimeError("VLM을 쓰려면 OPENAI_API_KEY 또는 GOOGLE_API_KEY 가 필요합니다 (.env)")
    cache.write_text(out, encoding="utf-8")
    return out


def add_figure_summaries(path: Path, records, resolution=110):
    import io
    with pdfplumber.open(path) as pdf:
        for r in records:
            if not r["needs_vision"]:
                continue
            page = pdf.pages[r["page"] - 1]
            img = page.crop(r["_bbox"]).to_image(resolution=resolution).original
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            out = describe_image(buf.getvalue())
            r["figure_summary"] = None if out.strip() in ("없음", "없음.") else out


# ------------------------------------------------------------------ CLI
def show(spec):
    fname, pno = spec.rsplit(":", 1)
    path = next(DATA.rglob(fname))
    recs, repeated = load_pdf(path)
    with pdfplumber.open(path) as pdf:
        raw = pdf.pages[int(pno) - 1].extract_text() or ""
    print("=" * 30, "기존 extract_text (앞 15줄)", "=" * 30)
    print("\n".join(raw.split("\n")[:15]))
    for r in [r for r in recs if r["page"] == int(pno)]:
        print("=" * 30, f"loader: half={r['half']} printed={r['printed_page']} layout={r['layout']} "
              f"needs_vision={r['needs_vision']}", "=" * 30)
        print(render_text(r)[:1500])
        if r["figure_lines"]:
            print("--- figure_lines(본문에서 분리된 차트 숫자) ---")
            print(" / ".join(r["figure_lines"][:12]))
    print("\n반복 줄로 제거된 패턴:", sorted(repeated)[:8])


def merge_stats(recs, p4l):
    if not p4l:
        return ""
    n_tab = sum(t.get("source") == "pymupdf4llm" for r in recs for t in r["tables"])
    n_head = sum(ln.startswith("## ") for r in recs for ln in r["text"].split("\n"))
    return f" p4l표={n_tab} 제목={n_head}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="파일명에 이 문자열이 포함된 PDF만")
    ap.add_argument("--vlm", action="store_true", help="차트·표 영역을 멀티모달 LLM으로 요약 (비용 발생)")
    ap.add_argument("--show", help="FILE.pdf:PAGE 한 페이지 결과 출력")
    ap.add_argument("--merge-p4l", action="store_true",
                    help="pymupdf4llm 제목·표를 합쳐 pages_v3.jsonl 로 저장 (src.compare_p4l 먼저)")
    args = ap.parse_args()
    if args.show:
        return show(args.show)

    OUT.mkdir(parents=True, exist_ok=True)
    all_recs = []
    p4l = load_p4l() if args.merge_p4l else None
    for path in sorted(DATA.rglob("*.pdf")):
        if args.only and args.only not in path.name:
            continue
        recs, repeated = load_pdf(path, p4l)
        if args.vlm:
            add_figure_summaries(path, recs)
        lay = Counter(r["layout"] for r in recs)
        print(f"{path.name[:40]:40s} regions={len(recs):4d} layout={dict(lay)} "
              f"tables={sum(len(r['tables']) for r in recs):3d} vision={sum(r['needs_vision'] for r in recs):3d} "
              f"repeated={len(repeated)}" + merge_stats(recs, p4l))
        all_recs.extend(recs)

    ver = "v3" if args.merge_p4l else "v2"
    out = OUT / (f"pages_{ver}.jsonl" if not args.only else f"pages_{ver}_{args.only}.jsonl")
    with open(out, "w", encoding="utf-8") as f:
        for r in all_recs:
            r = {k: v for k, v in r.items() if not k.startswith("_")}
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n{len(all_recs)} regions -> {out}")


if __name__ == "__main__":
    main()
