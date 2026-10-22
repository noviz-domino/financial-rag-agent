"""각주 풀이를 본문의 각주 번호 자리에 넣는다 (D37). 글자 좌표가 남아 있는 단계(2단 읽기 전)에서 처리한다.

  본문  '…부동산금융익스포저4)를 …'          (번호: 본문 글자 바로 뒤, 본문의 0.85배 이하, 아랫선이 위로 붙음)
  쪽 아래 '4) 금융기관 또는 … 포괄한다.'      (풀이: 줄 첫머리 'N)', 본문의 0.7~0.95배 크기, 다음 줄로 이어짐)
  →     '…부동산금융익스포저(금융기관 또는 … 포괄한다.)를 …'  · 쪽 아래 풀이 줄은 지움

같은 영역(쪽·반쪽) 안에서 번호가 같은 것끼리만 짝짓는다. 짝이 없는 각주 번호(쪽을 넘어가는 각주, 표·그림 주석)는 남긴다.
예외로 앞 글자의 0.65배 미만인 아주 작은 위첨자이면서 쪽에 그 번호의 'N) …' 줄도 없으면 지운다(예전 위첨자 규칙).
짝이 없는 풀이와 본문에 붙지 않은 작은 숫자(차트 축, 출처 줄 '2024년')는 그대로 둔다 (F1).
제목처럼 본문 크기가 아닌 글자 뒤의 번호는 건드리지 않는다 (제목 인식이 원문 줄과 맞아야 해서).
"""
from __future__ import annotations

import re
from collections import Counter

DIGIT = re.compile(r"\d")
NOTE_START = re.compile(r"^(\d{1,2})\)\s*")


def body_size(chars, share=0.3) -> float:
    """본문 글자 크기: 충분히 많이 쓰인 크기(최다 크기 글자 수의 share 이상) 중 가장 큰 것.
    각주·표 글씨가 본문보다 많은 쪽(주택정책금융 p4: 7.6 > 9.47)에서도 본문을 고르고, 제목(글자 수 적음)은 빠진다."""
    sizes = Counter(round(c["size"], 1) for c in chars if c["text"].strip())
    if not sizes:
        return 0.0
    top = sizes.most_common(1)[0][1]
    return max(s for s, n in sizes.items() if n >= share * top)


def _is_body(c, body, tol=0.1):
    return abs(c["size"] - body) <= body * tol


def find_markers(chars, body, ratio=0.85, raise_pt=1.0):
    """본문 글자 바로 뒤에 붙은 작은 숫자(+')') = 각주 번호. [{n, chars, prev_body}]
    prev_body: 앞 글자가 본문 크기인가 (아니면 제목 등이라 풀이를 넣지 않음)."""
    by_top = {}
    for p in chars:
        by_top.setdefault(int(p["top"]), []).append(p)
    out, used = [], set()
    for c in sorted(chars, key=lambda c: (c["top"], c["x0"])):
        if id(c) in used or not DIGIT.match(c["text"]):
            continue
        near = [p for k in range(int(c["top"]) - 3, int(c["top"]) + 4) for p in by_top.get(k, [])]
        prev = [p for p in near if p is not c and abs(p["top"] - c["top"]) < 3 and -2 <= c["x0"] - p["x1"] < 3
                and p["text"].strip()]
        if not prev:
            continue
        p = max(prev, key=lambda p: p["x1"])
        if not (c["size"] <= p["size"] * ratio and c["bottom"] < p["bottom"] - raise_pt):
            continue
        run = [c]
        while run[-1]["text"] != ")":                          # PDF 글자 순서가 아니라 위치로 이어 붙임 ('31)')
            nxt = [d for d in near if d is not run[-1] and abs(d["size"] - c["size"]) < 0.2 and abs(d["top"] - c["top"]) < 1.5
                   and -0.5 <= d["x0"] - run[-1]["x1"] < 2 and (DIGIT.match(d["text"]) or d["text"] == ")")]
            if not nxt:
                break
            run.append(min(nxt, key=lambda d: d["x0"]))
        used.update(id(d) for d in run)
        digits = "".join(d["text"] for d in run if DIGIT.match(d["text"]))
        if len(digits) <= 2 and run[-1]["text"] == ")":        # ')' 가 있어야 각주 번호 ('햇살론15' 같은 작은 숫자 제외)
            out.append({"n": int(digits), "chars": run, "prev_body": _is_body(p, body), "ratio": c["size"] / p["size"]})
    return out


def _lines(chars, tol=2.0, gap=20.0):
    """글자 → 줄. 같은 높이라도 가로로 gap 이상 떨어지면 다른 줄 (2단의 좌·우 단)."""
    rows = []
    for c in sorted(chars, key=lambda c: (c["top"], c["x0"])):
        if rows and abs(c["top"] - rows[-1][0]["top"]) <= tol:
            rows[-1].append(c)
        else:
            rows.append([c])
    out = []
    for r in rows:
        r.sort(key=lambda c: c["x0"])
        seg = [r[0]]
        for c in r[1:]:
            if c["x0"] - seg[-1]["x1"] > gap:
                out.append(seg)
                seg = [c]
            else:
                seg.append(c)
        out.append(seg)
    return out


def _text(line):
    return "".join(c["text"] for c in line)


def find_notes(chars, body, lo=0.7, hi=0.95, skip=frozenset()):
    """줄 첫머리가 'N)' 인 작은 글씨 줄 + 같은 크기로 이어지는 줄 = 각주 풀이. {n: [(top, 풀이 문장, chars)]}
    skip: 본문 각주 번호 글자 id (작은 글씨라 따로 모으면 'N)' 줄처럼 보여서 뺌)."""
    small = [c for c in chars if body * lo <= c["size"] <= body * hi and id(c) not in skip]
    lines = sorted(_lines(small), key=lambda l: (l[0]["top"], l[0]["x0"]))
    notes, cur = {}, None
    for ln in lines:
        t = _text(ln)
        m = NOTE_START.match(t)
        if m:
            cur = {"n": int(m.group(1)), "top": ln[0]["top"], "x0": ln[0]["x0"], "bottom": ln[0]["bottom"],
                   "text": t[m.end():], "chars": list(ln)}
            notes.setdefault(cur["n"], []).append(cur)
            continue
        h = ln[0]["bottom"] - ln[0]["top"]
        # 이어지는 줄은 '4)' 다음 글자 위치로 들여쓰기됨 (첫 줄 96pt → 106~110pt). 들여쓰지 않은 줄(표 제목 등)은 끊음
        if cur and cur["x0"] + 3 <= ln[0]["x0"] <= cur["x0"] + 25 and 0 <= ln[0]["top"] - cur["bottom"] < 1.5 * h:
            cur["text"] += t                                  # 한글은 단어 중간에서도 줄이 바뀌므로 그대로 이어 붙임
            cur["chars"] += ln
            cur["bottom"] = ln[0]["bottom"]
        else:
            cur = None
    out = {}
    for n, vs in notes.items():
        for v in vs:
            t = re.sub(r"\s+", " ", v["text"]).strip()
            if t:
                out.setdefault(n, []).append((v["top"], t, v["chars"]))
    return out


def plan_footnotes(chars, ratio=0.85, raise_pt=1.0):
    """짝 맞추기. 반환: (번호 첫 글자 id → '(풀이)', 지울 글자 id 집합, 넣은 수, 짝 없는 번호 수)"""
    body = body_size(chars)
    if not body:
        return {}, set(), 0, 0
    markers = find_markers(chars, body, ratio, raise_pt)
    skip = frozenset(id(c) for m in markers for c in m["chars"])
    notes = find_notes(chars, body, skip=skip)
    any_note = set(find_notes(chars, body, lo=0.0, hi=10.0, skip=skip))   # 크기 상관없이 'N) …' 줄이 있는 번호
    replace, drop, used, unmatched = {}, set(), set(), 0
    for m in markers:
        if not m["prev_body"]:
            continue                                           # 제목 등 뒤의 번호는 그대로 둠
        top = m["chars"][0]["top"]
        cands = [x for x in notes.get(m["n"], []) if id(x[2][0]) not in used]
        below = [x for x in cands if x[0] > top]
        pick = min(below or cands, key=lambda x: abs(x[0] - top)) if cands else None
        if pick is None:
            unmatched += 1
            # 쪽을 넘어가는 각주·표 주석은 번호를 남겨 연결 고리를 유지. 예전 규칙에 해당하는 아주 작은 위첨자
            # (앞 글자의 0.65배 미만)이면서 쪽에 그 번호의 주석 줄도 없을 때만 지움 ('16.5²)' → '16.5')
            if m["ratio"] < 0.65 and m["n"] not in any_note:
                drop.update(id(c) for c in m["chars"])
            continue
        used.add(id(pick[2][0]))
        replace[id(m["chars"][0])] = f"({pick[1]})"
        drop.update(id(c) for c in m["chars"][1:])
        drop.update(id(c) for c in pick[2])
    return replace, drop, len(replace), unmatched


def inline_footnotes(region, ratio=0.85, raise_pt=1.0):
    """pdfplumber 영역에 적용. 각주 번호 글자의 text 를 '(풀이)' 로 바꾼 사본 글자로 교체하고 풀이 글자는 뺀다.
    반환: (새 영역, 넣은 수, 짝 없는 번호 수)"""
    chars = region.chars
    replace, drop, n_in, n_un = plan_footnotes(chars, ratio, raise_pt)
    if not replace and not drop:
        return region, 0, n_un
    out = region.filter(lambda o: True)
    objs = {k: list(v) for k, v in region.objects.items()}
    objs["char"] = [({**c, "text": replace[id(c)]} if id(c) in replace else c) for c in objs.get("char", [])
                    if id(c) not in drop]
    out._objects = objs
    return out, n_in, n_un
