"""표 찾기 → 손실 판정 → (손실이면) 병합 칸 채우기 → 그래도 깨지면 Gemini 이미지 변환 대상으로 표시.

라우팅 (기획서 3-2)
  pdfplumber find_tables → Markdown
    한글·숫자가 각각 90% 이상 남음 → status "simple"  (그대로 사용)
    아니오 → 병합 칸 채우기 + 행 머리글 반복 → 다시 판정
        통과 → status "filled"
        실패 → status "lossy"   (본문 텍스트 유지, Gemini 이미지 변환은 figures 단계에서 --no-vlm 이 아닐 때)
출처: noviz loader.py (extract_tables·손실 판정·세로 라벨). 병합 칸 채우기·긴 표 분할은 새로 작성.
"""
from __future__ import annotations

import re

TABLE_SETTINGS = [
    {},                                                              # 선이 있는 표 (기본)
    {"vertical_strategy": "lines", "horizontal_strategy": "text"},   # 세로선만 있는 표 (가이드북)
]
HANGUL = r"[가-힣]"
DIGIT = r"[0-9]"


# ------------------------------------------------------------------ 행 정리·Markdown
def _expand_subrows(row):
    """선 없이 줄바꿈으로만 나뉜 하위 행('자산\\n금융자산\\n부동산')을 여러 행으로 펼친다."""
    parts = [(c or "").split("\n") for c in row]
    n = max(len(p) for p in parts)
    if n > 1 and all(len(p) in (1, n) for p in parts):
        return [[p[i] if len(p) == n else (p[0] if i == 0 else "") for p in parts] for i in range(n)]
    return [[(c or "").replace("\n", " ") for c in row]]


def clean_rows(rows, expand=True):
    rows = [sub for r in rows for sub in (_expand_subrows(r) if expand else [[(c or "").replace("\n", " ") for c in r]])]
    rows = [[c.strip() for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    keep = [j for j in range(len(rows[0])) if any(j < len(r) and r[j] for r in rows)]
    return [[r[j] if j < len(r) else "" for j in keep] for r in rows]


def to_markdown(rows):
    head, *body = rows
    esc = lambda c: c.replace("|", "\\|")
    lines = ["| " + " | ".join(esc(c) for c in head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(esc(c) for c in r) + " |" for r in body]
    return "\n".join(lines)


# ------------------------------------------------------------------ 손실 판정
def loss_scores(raw: str, md: str) -> tuple[float, float]:
    """표 영역 원문 대비 Markdown 에 남은 (한글 비율, 숫자 비율). 원문에 없으면 1.0."""
    out = []
    for pat in (HANGUL, DIGIT):
        n_raw, n_md = len(re.findall(pat, raw)), len(re.findall(pat, md))
        out.append(1.0 if n_raw == 0 else n_md / n_raw)
    return out[0], out[1]


def is_lossy(raw: str, md: str, threshold: float = 0.9) -> bool:
    """한글·숫자를 따로 세어 하나라도 threshold 미만이면 손실 (숫자만 세면 '적립금' 같은 행 이름 누락을 못 잡는다)."""
    h, d = loss_scores(raw, md)
    return h < threshold or d < threshold


# ------------------------------------------------------------------ 병합 칸 채우기
def _cluster(vals, tol=2.0):
    vals = sorted(vals)
    out = [vals[0]]
    for v in vals[1:]:
        if v - out[-1] > tol:
            out.append(v)
    return out


def fill_grid(cells, text_of, tol=2.0, slot_text=None):
    """셀 bbox 목록으로 격자를 만들어, 여러 칸에 걸친 셀의 글자를 걸친 모든 칸에 채운다.
    cells: [(x0, top, x1, bottom)], text_of(bbox) -> 글자. 반환: 2차원 문자열 격자 (행 x 열)."""
    xs = _cluster([c[0] for c in cells] + [c[2] for c in cells], tol)
    ys = _cluster([c[1] for c in cells] + [c[3] for c in cells], tol)

    def span(lo, hi, edges):
        idx = [i for i in range(len(edges) - 1) if edges[i] >= lo - tol and edges[i + 1] <= hi + tol]
        return idx

    grid = [["" for _ in range(len(xs) - 1)] for _ in range(len(ys) - 1)]
    covered = [[False] * (len(xs) - 1) for _ in range(len(ys) - 1)]
    for c in cells:
        txt = text_of(c)
        for r in span(c[1], c[3], ys):
            for k in span(c[0], c[2], xs):
                grid[r][k], covered[r][k] = txt, True
    if slot_text:   # 셀 도형이 없는 칸(병합 칸의 나머지 부분): 칸에 걸친 글자를 걸친 모든 칸에 채운다
        for r in range(len(ys) - 1):
            for k in range(len(xs) - 1):
                if not covered[r][k]:
                    grid[r][k] = slot_text((xs[k], ys[r], xs[k + 1], ys[r + 1]))
    return grid


def repeat_row_headers(rows):
    """첫 열(행 묶음 라벨)이 비어 있고 다른 칸에 값이 있으면 위 행의 라벨을 반복한다 (rowspan 라벨 보정)."""
    out = [list(r) for r in rows]
    for i in range(1, len(out)):
        if not out[i][0] and any(out[i][1:]) and out[i - 1][0]:
            out[i][0] = out[i - 1][0]
    return out


def filled_rows(region, table):
    cells = [tuple(c) for c in table.cells]
    if not cells:
        return []

    def text_of(b):
        box = (max(b[0], region.bbox[0]), max(b[1], region.bbox[1]), min(b[2], region.bbox[2]), min(b[3], region.bbox[3]))
        if box[2] <= box[0] or box[3] <= box[1]:
            return ""
        return (region.within_bbox(box).extract_text() or "").strip()

    words = [w for w in region.extract_words()
             if not any(c[0] - 1 <= w["x0"] and w["x1"] <= c[2] + 1 and c[1] - 1 <= w["top"] and w["bottom"] <= c[3] + 1
                        for c in cells)]

    def slot_text(b):
        hit = []
        for w in words:
            ov = min(w["x1"], b[2]) - max(w["x0"], b[0])
            if ov >= 0.3 * min(w["x1"] - w["x0"], b[2] - b[0]) and b[1] - 1 <= (w["top"] + w["bottom"]) / 2 <= b[3] + 1:
                hit.append(w["text"])
        return " ".join(hit)

    grid = fill_grid(cells, text_of, slot_text=slot_text)
    rows = clean_rows(grid)
    return repeat_row_headers(rows) if rows else []


# ------------------------------------------------------------------ 긴 표 나누기
def split_table_md(md: str, max_chars: int, caption: str = "") -> list[str]:
    """긴 표를 행 단위로 나누고 조각마다 캡션·머리글(2줄)을 반복한다."""
    lines = md.split("\n")
    head, rows = lines[:2], lines[2:]
    prefix = (f"[표] {caption}\n" if caption else "[표]\n") + "\n".join(head)
    parts, cur = [], []
    for r in rows:
        if cur and len(prefix) + sum(len(x) + 1 for x in cur) + len(r) > max_chars:
            parts.append(cur)
            cur = []
        cur.append(r)
    if cur or not parts:
        parts.append(cur)
    return [prefix + ("\n" + "\n".join(p) if p else "") for p in parts]


# ------------------------------------------------------------------ 표 찾기
def extract_tables(region, threshold: float = 0.9):
    rw, rh = region.bbox[2] - region.bbox[0], region.bbox[3] - region.bbox[1]
    for settings in TABLE_SETTINGS:
        found = []
        for t in region.find_tables(settings):
            rows = clean_rows(t.extract())
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
            above = region.within_bbox((max(region.bbox[0], x0 - 5), max(region.bbox[1], top - 40),
                                        min(region.bbox[2], x1 + 5), top))
            caption = " ".join((above.extract_text() or "").split("\n")[-2:]).strip()
            raw = region.within_bbox(t.bbox).extract_text() or ""
            md = to_markdown(rows)
            status, source = "simple", "pdf_table"
            if is_lossy(raw, md, threshold):
                try:
                    frows = filled_rows(region, t)
                except Exception:  # noqa: BLE001 - 격자 복원 실패는 lossy 로 둔다
                    frows = []
                fmd = to_markdown(frows) if len(frows) >= 2 and len(frows[0]) >= 2 else ""
                if fmd and not is_lossy(raw, fmd, threshold):
                    md, status, source = fmd, "filled", "pdf_table_filled"
                else:
                    status = "lossy"
            found.append({"bbox": t.bbox, "caption": caption, "markdown": md, "lossy": status == "lossy",
                          "status": status, "source": source})
        if found:
            return found
    return []


# ------------------------------------------------------------------ 표 옆 세로 라벨
def attach_vertical_labels(tables, vruns):
    """표 옆의 세로 구분 라벨(예: 주택연금 p.72 '가입가능'/'가입불가')을 표의 '구분' 열로 붙인다.
    라벨 하나가 표 높이의 절반 이상을 덮을 때만 모든 행에 붙이고, 아니면 캡션에만 표시한다."""
    for t in tables:
        x0, top, x1, bottom = t["bbox"]
        labels, covers_all = [], False
        for (rx0, rtop, rx1, rbottom), text in vruns:
            if not re.search(r"[가-힣A-Za-z]", text) or rx0 >= x1:
                continue
            overlap = min(bottom, rbottom) - max(top, rtop)
            if overlap >= 0.5 * (rbottom - rtop) and rx1 <= x1:
                labels.append(text.replace(" ", ""))
                covers_all = (rbottom - rtop) >= 0.5 * (bottom - top)
        if len(labels) == 1 and covers_all:
            lines = t["markdown"].split("\n")
            lines[0] = "| 구분 " + lines[0]
            lines[1] = "|---" + lines[1]
            lines[2:] = [f"| {labels[0]} " + ln for ln in lines[2:]]
            # 첫 행도 데이터인 표가 많아(p.72) 머리글 줄에는 '구분' 열 이름, 값은 모두 같은 라벨
            t["markdown"] = "\n".join(lines)
            t["caption"] = f"{t['caption']} [구분: {labels[0]}]".strip()
            t["group_label"] = labels[0]
        elif labels:
            t["caption"] = f"{t['caption']} [구분: {' / '.join(labels)}]".strip()
            t["group_label"] = " / ".join(labels)
    return tables
