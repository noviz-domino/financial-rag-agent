"""PDF → 쪽(반쪽) 레코드. 읽는 순서가 맞는 영역 단위로 글자를 뽑고 노이즈를 정리한다.

출처: noviz loader.py (pymupdf4llm 합치기·VLM 부분 제외). 새로 넣은 규칙: cid·특수 글리프 복구, 쪽 번호 제거.

  ① 펼침면 분리 (가로 > 세로 x 1.15 → 좌/우 반쪽, page 는 그대로·half 만 L/R)
  ② 굵은 글씨 겹침 복원 → 세로 글자 한 줄로 보존 → 쪽 번호 제거 → 각주 풀이를 본문 번호 자리에 '(풀이)' 로 (D38)
  ③ 표 (src/tables) ④ 2단이면 좌단 → 우단 순서  ⑤ 머리말·꼬리말(위·아래 3줄에서 30% 이상 반복) 제거
출력: data/pages.jsonl — file, doc_type, page, half, printed_page, text, tables[], figure_lines[], has_text_layer
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pdfplumber

from src.loading.footnotes import inline_footnotes
from src.tables.extract import attach_vertical_labels, extract_tables

NUM_TOKEN = re.compile(r"^[\(\[]?[-+~]?[\d.,]*\d[\d.,]*[%p]?[\)\]]?$|^\(?[%‰]\)?$|^\((조|억|만)?원\)$|^\(%p?\)$")
HANGUL_WORD = re.compile(r"[가-힣]{2,}")
NOISE_LINE = re.compile(r"^\d{4}\.\s*\d+\.\s*\d+\.\s*(?:오전|오후)")   # 인쇄 시각 줄
EDGE_NUM = re.compile(r"^(\d{1,3})\s+\S.*|^.*\S\s+(\d{1,3})$")           # '33 …' / '… 33' (쪽 번호가 붙은 머리말·꼬리말)
PAGE_NO = re.compile(r"^[-–—\s]*\d{1,3}[-–—\s]*$")
CID = re.compile(r"\(cid:\d+\)")
PUA_LEAD = re.compile(r"^(\s*)[-]+\s*")
PUA_BETWEEN = re.compile(r"(?<=[가-힣])[-](?=[가-힣])")
PUA_ANY = re.compile(r"[-]")


def doc_type_of(path: Path) -> str:
    if path.name == "financial_glossary_readable.pdf":
        return "glossary"
    if path.name == "financial_life_guide.pdf":
        return "life_guide"
    if path.parent.name == "standard_terms":
        return "terms"
    return "additional"


# ------------------------------------------------------------------ 깨진 글자
def clean_line(line: str) -> str:
    """cid 깨짐·사용자 정의 영역(PUA) 특수 글리프 복구. 줄 맨 앞은 불릿 '•', 한글 사이는 '·', 나머지는 삭제."""
    line = PUA_BETWEEN.sub("·", line)
    line = PUA_LEAD.sub(r"\1• ", line)
    line = PUA_ANY.sub("", line)
    if CID.search(line):
        line = CID.sub("•", line) if line.lstrip().startswith("(cid:") else CID.sub("", line)
    return line


def clean_text(text: str) -> str:
    return "\n".join(clean_line(ln) for ln in text.split("\n"))


# ------------------------------------------------------------------ 영역 유틸
def _inside(obj, box, pad=1.0):
    x0, top, x1, bottom = box
    cx, cy = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
    return x0 - pad <= cx <= x1 + pad and top - pad <= cy <= bottom + pad


def drop_chars_in(region, boxes):
    if not boxes:
        return region
    return region.filter(lambda o: not (o.get("object_type") == "char" and any(_inside(o, b) for b in boxes)))


def split_spread(page, ratio=1.15):
    if page.width > page.height * ratio:
        w = page.width / 2
        return [("L", page.crop((0, 0, w, page.height))), ("R", page.crop((w, 0, page.width, page.height)))]
    return [(None, page)]


def vertical_runs(region, min_run=4):
    """같은 x0(±2pt)에 좌우가 빈 1~2글자 단어가 세로로 min_run 개 이상 연속 → 세로쓰기. [(bbox, 이어 붙인 글자)]
    지우지 않고 '[세로] …' 한 줄로 남긴다 (반복 사이드바는 머리말 규칙이 제거, 표 라벨은 남음)."""
    all_words = region.extract_words()

    def isolated(w, gap=12):
        return not any(o is not w and abs(o["top"] - w["top"]) < 3 and
                       (0 <= o["x0"] - w["x1"] < gap or 0 <= w["x0"] - o["x1"] < gap) for o in all_words)

    def joined(run):
        out = run[0]["text"]
        for a, b in zip(run, run[1:]):
            out += (" " if b["top"] - a["bottom"] > 6 else "") + b["text"]
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


# ------------------------------------------------------------------ 단 분리
def group_lines(words, tol=3):
    lines = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(w["top"] - lines[-1][0]["top"]) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    return lines


def crosses(line, gx, min_gap=8):
    if any(w["x0"] < gx < w["x1"] for w in line):
        return True
    lefts = [w["x1"] for w in line if w["x1"] <= gx]
    rights = [w["x0"] for w in line if w["x0"] >= gx]
    return bool(lefts and rights) and min(rights) - max(lefts) < min_gap


def find_gutter(words, x0, x1, steps=48):
    """가운데 38~62% 구간에서 '여백 없이 가로지르는 줄'이 가장 적은 x. 줄 수 기준 30% 미만이면 2단."""
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
    """2단이면 '전체 폭 띠 / 2단 띠'로 나눠 위에서 아래로, 2단 띠는 좌→우 순서."""
    x0, top, x1, bottom = region.bbox
    words = region.extract_words()
    gx = find_gutter(words, x0, x1)
    if gx is None:
        return region.extract_text() or "", "1col"
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


# ------------------------------------------------------------------ 줄 정리
def is_print_slug(line):
    return ".indd" in line or "iinndddd" in line


def is_axis_line(line):
    toks = line.split()
    if len(toks) < 2 or HANGUL_WORD.search(line):
        return False
    return sum(1 for t in toks if NUM_TOKEN.match(t)) / len(toks) >= 0.6


def _margin_numbers(region, top_frac=0.07, bottom_frac=0.09):
    """쪽 위·아래 여백에 혼자 놓인 1~3자리 숫자 단어 = 쪽 번호."""
    x0, top, x1, bottom = region.bbox
    h = bottom - top
    words = region.extract_words()
    out = []
    for w in words:
        if not re.fullmatch(r"[0-9]{1,3}", w["text"]):
            continue
        if not (w["top"] > bottom - bottom_frac * h or w["bottom"] < top + top_frac * h):
            continue
        near = [o for o in words if o is not w and abs(o["top"] - w["top"]) < 3 and abs(o["x0"] - w["x1"]) < 40]
        if not near or all(re.fullmatch(r"[0-9]{1,3}", o["text"]) for o in near):
            out.append(w)
    return out


def printed_page_of(region):
    ws = _margin_numbers(region)
    if not ws:
        return None
    x0, top, x1, bottom = region.bbox
    mid = top + (bottom - top) / 2
    return int(max(ws, key=lambda w: abs(w["top"] - mid))["text"])


def drop_page_numbers(region):
    ws = _margin_numbers(region)
    return drop_chars_in(region, [(w["x0"], w["top"], w["x1"], w["bottom"]) for w in ws])


def norm_line(line):
    return re.sub(r"\d+", "#", line.strip())


def drop_superscripts(region, ratio=0.65):
    """(예전 규칙, 파이프라인에서는 src/loading/footnotes.py 로 바뀜 — D38) 쪽 중앙값 x ratio 미만의 숫자·괄호를 지움.
    각주 번호(본문의 약 0.75배)는 못 지우고 차트 축 숫자·출처 줄 '2024'는 지우는 문제가 있었다 (F1)."""
    sizes = sorted(c["size"] for c in region.chars)
    if not sizes:
        return region
    limit = sizes[len(sizes) // 2] * ratio
    return region.filter(lambda o: not (o.get("object_type") == "char" and o["size"] < limit
                                        and re.match(r"[\d)\]*]", o["text"])))


def process_region(page_no, half, region, cfg=None):
    lc = (cfg or {}).get("loading", {})
    tc = (cfg or {}).get("tables", {})
    region = region.dedupe_chars()                      # '1100..55%%' → '10.5%'
    printed = printed_page_of(region)
    vruns = vertical_runs(region)
    region = drop_page_numbers(region)
    region = drop_chars_in(region, [b for b, _ in vruns])
    fc = lc.get("footnote", {})
    n_fn = n_fn_unmatched = 0
    if fc.get("enabled", True):                         # 각주 풀이를 본문 번호 자리에 '(풀이)' 로 (D38)
        region, n_fn, n_fn_unmatched = inline_footnotes(region, fc.get("marker_ratio", 0.85), fc.get("raise_pt", 1.0))
    else:
        region = drop_superscripts(region, lc.get("superscript_ratio", 0.65))
    tables = attach_vertical_labels(extract_tables(region, tc.get("loss_threshold", 0.9)), vruns)
    # 깨끗하게(simple·filled) 뽑힌 표만 본문에서 빼고 Markdown 으로 대체. lossy 표는 본문을 그대로 두어 손실·중복을 막는다
    body = drop_chars_in(region, [t["bbox"] for t in tables if not t["lossy"]])
    text, layout = read_in_order(body)

    lines, fig = [], []
    for ln in clean_text(text).split("\n"):
        s = ln.strip()
        if not s or PAGE_NO.match(s) or is_print_slug(s) or NOISE_LINE.match(s):
            continue
        (fig if is_axis_line(s) else lines).append(s)
    lines += [f"[세로] {t}" for _, t in vruns if re.search(r"[가-힣A-Za-z]", t)]
    return {"page": page_no, "half": half, "printed_page": printed, "layout": layout, "lines": lines,
            "tables": [{"caption": clean_text(t["caption"]), "markdown": clean_text(t["markdown"]), "lossy": t["lossy"],
                        "status": t["status"], "source": t["source"], "bbox": [round(v, 1) for v in t["bbox"]],
                        **({"group_label": t["group_label"]} if t.get("group_label") else {})} for t in tables],
            "figure_lines": fig, "footnotes": {"inlined": n_fn, "unmatched": n_fn_unmatched}, "_bbox": region.bbox}


def remove_repeated(records, min_ratio=0.3, min_count=3, edge=3):
    """머리/꼬리 위치(앞뒤 edge 줄)에서 min_ratio 이상의 영역에 반복되는 줄(머리말·꼬리말·워터마크)을 제거.
    위치를 제한해 '자료: 한국은행 …' 같은 본문 속 반복 줄은 지우지 않는다. 반환: 지운 패턴 집합."""
    cnt = Counter()
    for r in records:
        cnt.update({norm_line(l) for l in r["lines"][:edge] + r["lines"][-edge:]})
    limit = max(min_count, min_ratio * len(records))
    repeated = {k for k, v in cnt.items() if v >= limit}
    # 쪽 번호가 붙은 머리말·꼬리말('Ⅲ 소득과 지출관리 33'): 가장자리 2줄, 숫자만 달라지며 서로 다른 쪽 3곳 이상에 반복
    nums = {}
    for r in records:
        for l in r["lines"][:2] + r["lines"][-2:]:
            m = EDGE_NUM.match(l.strip())
            if m and len(l) <= 40:
                nums.setdefault(norm_line(l), set()).add(m.group(1) or m.group(2))
    repeated |= {k for k, v in nums.items() if len(v) >= 3}
    for r in records:
        r["lines"] = [l for l in r["lines"] if norm_line(l) not in repeated]
    return repeated


def load_pdf(path: Path, cfg=None):
    """반환: (쪽 레코드 목록, 반복 줄 패턴). page = PDF 쪽 순서 1부터."""
    lc = (cfg or {}).get("loading", {})
    records = []
    with pdfplumber.open(path) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            for half, region in split_spread(page, lc.get("spread_ratio", 1.15)):
                records.append(process_region(page_no, half, region, cfg))
    edge, ratio = lc.get("repeat_edge_lines", 3), lc.get("repeat_ratio", 0.3)
    repeated = remove_repeated(records, ratio, 3, edge) if len(records) >= 3 else set()
    dt = doc_type_of(path)
    out = []
    for i, r in enumerate(records):
        r["text"] = "\n".join(r.pop("lines"))
        r["has_text_layer"] = bool(r["text"].strip() or r["tables"] or r["figure_lines"])
        for j, t in enumerate(r["tables"]):
            t["table_id"] = f"{path.stem}__p{r['page']}{r['half'] or ''}__t{j}"
        r.update(file=path.name, doc_type=dt)
        out.append(r)
    return out, repeated
