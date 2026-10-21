"""청킹 전략 모듈: 구조형(structured) vs 재귀(recursive). 같은 추출 결과(pipeline/extracted)에서 청크 JSONL을 만든다.

청크 형식(search_tool.py 호환): chunk_id, file, page, text  + 메타데이터
  page: 청크가 시작하는 쪽,  pages: 청크가 걸친 쪽 목록,  text_raw: 맥락 접두어를 붙이기 전 본문
  doc_title, group(glossary | terms | guide), section(제목 경로), kind(term | article | body | table | figure), strategy

전략
  recursive  : 쪽 단위 본문(표는 Markdown, 그림은 아래 규칙)을 재귀 분할(문단 → 줄 → 공백). chunk_size, overlap 지정. 쪽을 넘지 않는다.
  structured : 문서 종류별로 의미 단위로 나눈다.
      glossary(용어집)    용어 하나 = 청크 하나(PDF 글꼴: 용어 11pt, 정의 10pt)
      terms(표준약관)      조항(제N조) = 청크 하나, 길면 항(①②…) 경계로 분할하고 조 제목을 반복
      guide(가이드북·추가)  글꼴 크기로 찾은 제목 단위. 작은 절은 다음 절과 합치고, 길면 문단 경계로 분할. 표는 따로 청크(길면 머리글 반복)
옵션 context_prefix=True: 청크 앞에 '[문서명 > 제목 경로]'를 붙인다(문맥 보존 실험).

그림 블록 규칙: Gemini 비전 판정이 있으면 장식 그림은 제외, OCR 품질이 poor/none이면 OCR 글 대신 설명을 쓴다. 판정이 없으면 글자층 글 그대로.
"""
import argparse, glob, json, os, re, sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent
EXTRACTED = ROOT / "pipeline" / "extracted"
DATA = ROOT / "pipeline" / "data"
NL = chr(10)

DOC_TITLES = {
    "financial_glossary_readable.pdf": "금융용어 모음",
    "financial_life_guide.pdf": "20대 사회첫걸음 금융생활 가이드북",
    "housing_pension_2026.pdf": "2026 주택연금 백문백답",
    "housing_policy_finance_2025.pdf": "주택 정책금융의 현황과 효과(한국은행)",
    "insurance_terms_example_2020.pdf": "쉽게 이해하는 약관 요약서 모범 예시",
    "policy_finance_debt_adjustment_2026.pdf": "정책서민금융·채무조정 분석",
    "retirement_pension_2025.pdf": "퇴직연금 현황(2025)",
    "self_employed_debt_2025.pdf": "자영업 가구의 채무상환능력 평가(한국은행)",
}
BULLET = re.compile(r"^(?:[■◦○●◆▶▷·※•□▪◇\-*]|[①-⑳]|\(\d+\)\s|\d+[.)]\s|[가-힣][.)]\s|[-])")
NOT_HEAD = re.compile(r"^(?:\[표|\[그림|<출처|자료\s*[:：]|주\s*[:：)]|출처\s*[:：]|※)")


# ------------------------------------------------------------------ 추출 결과 읽기
def list_docs():
    docs = []
    for jl in sorted(glob.glob(str(EXTRACTED / "*" / "*.jsonl"))):
        p = Path(jl)
        docs.append(dict(domain=p.parent.name, file=p.name[:-len(".jsonl")], path=p))
    return docs


def load_pages(doc):
    return [json.loads(l) for l in open(doc["path"], encoding="utf-8") if l.strip()]


def doc_group(doc):
    return {"glossary": "glossary", "standard_terms": "terms"}.get(doc["domain"], "guide")


def doc_title(doc, pages):
    if doc["domain"] == "standard_terms":
        first = pages[0]["blocks"][0]["text"].split(NL)[0].strip() if pages and pages[0]["blocks"] else doc["file"]
        return first
    return DOC_TITLES.get(doc["file"], doc["file"])


CAP = re.compile(r"^(?:\[(?:표|그림)|\((?:단위|기준)|<(?:표|그림))")
NOTE = re.compile(r"^(?:자료\s*[:：]|주\s*[:：)]|출처\s*[:：]|<출처|※)")
NOISE_LINE = re.compile(r"\.indd\b|^\d{4}\.\s*\d+\.\s*\d+\.\s*(?:오전|오후)")
HANGUL_PUA = re.compile(r"(?<=[가-힣])[-](?=[가-힣])")


def undouble(line):
    """글꼴 효과로 글자가 두 번씩 찍힌 줄('2299..77%%')을 풀어 준다. 길이 4 이상인 낱말 둘 이상이 두 번씩 찍혔을 때만."""
    toks = line.split(" ")
    dbl = [len(t) >= 4 and len(t) % 2 == 0 and t[0::2] == t[1::2] for t in toks]
    if sum(dbl) < 2:
        return line
    return " ".join(t[::2] if len(t) >= 2 and len(t) % 2 == 0 and t[0::2] == t[1::2] else t for t in toks)


PUA_LEAD = re.compile(r"^(\s*)[-]+\s*")
PUA_ANY = re.compile(r"[-]")


def clean_text(s):
    """글자 겹침·인쇄 메타 줄 정리 + 사용자 정의 영역(PUA) 글리프 정리.
    줄 맨 앞 PUA 글리프(불릿 기호로 쓰인 것)는 '•'로, 한글 사이의 것은 가운뎃점 '·'으로 바꾸고 나머지는 지운다."""
    s = HANGUL_PUA.sub("·", s)
    out = []
    for ln in s.split(NL):
        ln = undouble(ln)
        if NOISE_LINE.search(ln):
            continue
        ln = PUA_ANY.sub("", PUA_LEAD.sub(r"\1• ", ln))
        out.append(ln)
    return NL.join(out)


def is_toc(text):
    """목차처럼 줄 대부분이 쪽 번호로 끝나는 글."""
    lines = [l.strip() for l in text.split(NL) if l.strip()]
    if len(lines) < 8:
        return False
    hit = sum(1 for l in lines if re.search(r"\s\d{1,3}$", l) or re.fullmatch(r"\d{1,3}", l))
    return hit / len(lines) >= 0.5


def clean_md(s):
    return re.sub(r"\*\*|__", "", s)


def figure_text(b):
    v = b.get("meta", {}).get("vision")
    t = clean_text(b["text"].strip())
    if v:
        if v["is_decorative"]:
            return None
        return t if v["ocr_quality"] == "good" else "[그림 설명] " + v["description"]
    return t if len(t) >= 8 else None


def table_text(b):
    parts = []
    cap = b.get("meta", {}).get("caption")
    if cap:
        parts.append(cap)
    parts.append(b["text"])
    note = b.get("meta", {}).get("note")
    if note:
        parts.append(note)
    return NL.join(parts)


def block_text(b):
    if b["type"] == "table":
        return table_text(b)
    if b["type"] in ("image_text", "chart_text"):
        return figure_text(b)
    if b.get("source") == "vision_description":
        return "[그림 설명] " + b["text"].strip()
    return clean_text(clean_md(b["text"])).strip()


def same_page(a, b):
    return a["file"] == b["file"] and a["page"] == b["page"]


def merge_short(chunks, min_len=100):
    """본문·그림 청크 중 min_len 미만인 것을 같은 쪽 이웃에 합친다(앞 청크 뒤에, 앞이 없으면 뒤 청크 앞에).
    짧은 청크(캡션·출처·주석·쪽 번호·그림 속 숫자)는 답이 거의 없으면서 검색 상위를 차지하기 때문이다."""
    res, i = [], 0
    while i < len(chunks):
        c = chunks[i]
        if c["kind"] in ("body", "figure") and len(c["text_raw"]) < min_len:
            if res and same_page(res[-1], c):
                res[-1]["text_raw"] += NL + c["text_raw"]
                res[-1]["merged_short"] = res[-1].get("merged_short", 0) + 1
                i += 1
                continue
            if i + 1 < len(chunks) and same_page(chunks[i + 1], c):
                nxt = chunks[i + 1]
                nxt["text_raw"] = c["text_raw"] + NL + nxt["text_raw"]
                nxt["merged_short"] = nxt.get("merged_short", 0) + 1
                i += 1
                continue
        res.append(c)
        i += 1
    return res


def split_table(b, max_chars, pre=(), post=()):
    """표 하나를 max_chars 이하 조각 목록으로(길면 행 단위로 나누고 머리글 두 줄을 반복). pre/post: 앞 캡션·뒤 각주 줄."""
    cap = NL.join(list(pre) + ([b["meta"]["caption"]] if b.get("meta", {}).get("caption") else [])) or None
    note = NL.join(([b["meta"]["note"]] if b.get("meta", {}).get("note") else []) + list(post)) or None
    lines = b["text"].split(NL)
    head, rows = lines[:2], lines[2:]
    out, cur = [], []
    for r in rows:
        if cur and sum(len(x) + 1 for x in head + cur) + len(r) > max_chars:
            out.append(head + cur)
            cur = []
        cur.append(r)
    out.append(head + cur)
    texts = []
    for i, ls in enumerate(out):
        t = NL.join(ls)
        if cap and i == 0:
            t = cap + NL + t
        if note and i == len(out) - 1:
            t = t + NL + note
        texts.append(t)
    return texts


def hard_split(text, max_chars):
    """max_chars를 넘는 글을 줄 경계 우선으로 자른다."""
    if len(text) <= max_chars:
        return [text]
    out, cur = [], ""
    for ln in text.split(NL):
        while len(ln) > max_chars:
            if cur:
                out.append(cur)
                cur = ""
            out.append(ln[:max_chars])
            ln = ln[max_chars:]
        if cur and len(cur) + len(ln) + 1 > max_chars:
            out.append(cur)
            cur = ""
        cur = cur + NL + ln if cur else ln
    if cur:
        out.append(cur)
    return out


# ------------------------------------------------------------------ 재귀 청킹
def recursive_split(text, size, overlap, seps=(NL + NL, NL, " ", "")):
    """langchain의 RecursiveCharacterTextSplitter와 같은 방식(큰 구분자부터 나누고 size 이하로 합침, 겹침 유지)."""
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
        sp = RecursiveCharacterTextSplitter(chunk_size=size, chunk_overlap=overlap, separators=list(seps))
        return [c for c in sp.split_text(text) if c.strip()]
    except ImportError:
        return [c for c in hard_split(text, size) if c.strip()]


def strip_sources(lines):
    """'출처 목록' 이후와 URL만 있는 출처 줄은 검색 대상이 아니다."""
    out = []
    for ln in lines:
        if re.match(r"^출처 목록", ln.strip()):
            break
        out.append(ln)
    return out


def page_plain_text(rec):
    parts = []
    for b in rec["blocks"]:
        t = block_text(b)
        if t:
            parts.append(t)
    text = (NL + NL).join(parts)
    return NL.join(strip_sources(text.split(NL))) if "출처 목록" in text else text


def merge_tail(parts, overlap, tail_min=100):
    """마지막 조각이 tail_min 미만이면 앞 조각에 합친다(겹친 부분은 한 번만 남긴다)."""
    if len(parts) > 1 and len(parts[-1]) < tail_min:
        prev, tail = parts[-2], parts[-1]
        for k in range(min(overlap, len(prev), len(tail)), 0, -1):
            if prev.endswith(tail[:k]):
                tail = tail[k:]
                break
        parts = parts[:-2] + [prev + tail]
    return parts


def chunk_recursive(doc, pages, size=500, overlap=50, context_prefix=False, tail_min=100, drop_below=30):
    title, group = doc_title(doc, pages), doc_group(doc)
    chunks = []
    for rec in pages:
        txt = page_plain_text(rec)
        if doc["domain"] == "glossary" and "/ 용어 설명 /" in txt:
            continue
        parts = merge_tail(recursive_split(txt, size, overlap), overlap, tail_min)
        for part in parts:
            if len(part.strip()) < drop_below:       # 너무 짧은 조각(쪽 번호 등)은 버린다
                continue
            chunks.append(dict(file=doc["file"], page=rec["page"], pages=[rec["page"]], text_raw=part, doc_title=title,
                               group=group, section="", kind="body"))
    return finish(chunks, doc, title, context_prefix, "recursive")


# ------------------------------------------------------------------ 공통 마무리
def finish(chunks, doc, title, context_prefix, strategy):
    stem = Path(doc["file"]).stem
    out, seq = [], Counter()
    chunks = [c for c in chunks if not (c["group"] == "guide" and c["kind"] == "body" and is_toc(c["text_raw"]))]   # 목차 제외
    seen, uniq = {}, []                       # 같은 파일 안의 완전히 같은 청크(공백 무시, 20자 이상)는 처음 것만 남기고 쪽을 dup_pages에 적는다
    for c in chunks:
        key = re.sub(r"\s+", "", c["text_raw"])
        if len(key) >= 20 and key in seen:
            seen[key].setdefault("dup_pages", []).append(c["page"])
            continue
        seen[key] = c
        uniq.append(c)
    chunks = uniq
    for c in chunks:
        raw = c.pop("text_raw")
        prefix = ""
        if context_prefix:
            prefix = "[" + " > ".join(x for x in [title, c.get("section")] if x) + "]" + NL
        n = seq[(c["file"], c["page"])]
        seq[(c["file"], c["page"])] += 1
        # id = 파일|p쪽|c번호: 다시 색인해도 같은 쪽의 같은 순번 청크가 같은 id를 갖는다
        out.append(dict(chunk_id=f"{c['file']}|p{c['page']}|c{n}", file=c.pop("file"), page=c.pop("page"), text=prefix + raw,
                        text_raw=raw, strategy=strategy, **c))
    return out


# ------------------------------------------------------------------ 구조형: 용어집
def glossary_terms():
    import pdfplumber
    pdf = pdfplumber.open(str(DATA / "glossary" / "financial_glossary_readable.pdf"))
    terms, cur, stop = [], None, False
    for pno, pg in enumerate(pdf.pages, 1):
        for ln in pg.extract_text_lines(return_chars=True, strip=True):
            cs = [c for c in ln["chars"] if c["text"].strip()]
            text = ln["text"].strip()
            if not cs or re.fullmatch(r"\d{1,3}", text) or text == "금융용어 모음":
                continue
            if re.match(r"^출처 목록", text) or "/ 용어 설명 /" in text:
                stop = True
            if stop:
                break
            sz = Counter(round(c["size"], 1) for c in cs).most_common(1)[0][0]
            if 10.8 <= sz <= 12:
                cur = dict(term=text, defn=[], page=pno)
                terms.append(cur)
            elif cur is not None:
                cur["defn"].append(text)
                cur.setdefault("pages", set()).add(pno)
        if stop:
            break
    return terms


def chunk_glossary(doc, pages, context_prefix=False):
    title = DOC_TITLES[doc["file"]]
    chunks = []
    for t in glossary_terms():
        body = t["term"] + NL + NL.join(t["defn"])
        chunks.append(dict(file=doc["file"], page=t["page"], pages=sorted({t["page"]} | t.get("pages", set())), text_raw=body,
                           doc_title=title, group="glossary", section=t["term"], kind="term", term=t["term"]))
    return finish(chunks, doc, title, context_prefix, "structured")


# ------------------------------------------------------------------ 구조형: 약관
ART = re.compile(r"^제\d+조(?:의\d+)?\(")
SECTION_HEAD = re.compile(r"^(?:제\d+조(?:의\d+)?\(|부\s*칙|[<\[]?별첨|<?별표)")
CHAPTER = re.compile(r"^제\d+장\s")
CLAUSE = re.compile(r"^[①-⑳]")


def chunk_terms(doc, pages, max_chars=1200, context_prefix=False):
    title = doc_title(doc, pages)
    lines = []                                   # (쪽, 줄)
    for rec in pages:
        for b in rec["blocks"]:
            if b["type"] in ("text", "scan_text"):
                for ln in clean_md(b["text"]).split(NL):
                    if ln.strip():
                        lines.append((rec["page"], ln.strip()))
    cut = next((i for i, (_, ln) in enumerate(lines) if re.match(r"^출처 목록", ln)), len(lines))
    lines = lines[:cut]
    # 첫 쪽 머리(약관명·기준일 줄)는 본문이 아니다
    while lines and not SECTION_HEAD.match(lines[0][1]) and not CHAPTER.match(lines[0][1]):
        lines.pop(0)
    sections, cur, chapter = [], None, ""
    for pno, ln in lines:
        if CHAPTER.match(ln):
            chapter = ln
            continue
        if SECTION_HEAD.match(ln):
            cur = dict(head=ln, page=pno, pages={pno}, lines=[ln], chapter=chapter)
            sections.append(cur)
        elif cur is not None:
            cur["lines"].append(ln)
            cur["pages"].add(pno)
    chunks = []
    for s in sections:
        label = re.match(r"^(제\d+조(?:의\d+)?\([^)]*\)|부\s*칙|[<\[]?별첨[>\]]?|<?별표[^ ]*)", s["head"])
        label = label.group(1) if label else s["head"][:20]
        sect_path = " > ".join(x for x in [s["chapter"], label] if x)
        body = NL.join(s["lines"])
        pieces = [body]
        if len(body) > max_chars:                # 항(①②…) 경계로 나눠 조 제목을 반복한다
            pieces, cur_p = [], []
            for ln in s["lines"][1:]:
                if CLAUSE.match(ln) and cur_p and len(NL.join(cur_p)) + len(ln) > max_chars - len(label) - 1:
                    pieces.append(NL.join([label] + cur_p))
                    cur_p = []
                cur_p.append(ln)
            pieces.append(NL.join([s["head"]] + cur_p if not pieces else [label] + cur_p))
            pieces = [x for p_ in pieces for x in hard_split(p_, max_chars + 400)]
        for k, piece in enumerate(pieces):
            chunks.append(dict(file=doc["file"], page=s["page"], pages=sorted(s["pages"]), text_raw=piece, doc_title=title,
                               group="terms", section=sect_path, kind="article", article=label, part=k))
    return finish(chunks, doc, title, context_prefix, "structured")


# ------------------------------------------------------------------ 구조형: 가이드북·추가 문서 (제목·문단)
def heading_map(pdf_path):
    """PDF 글꼴로 제목 줄을 찾는다. 반환: {쪽: {공백 없는 글: 단계(1~3)}}."""
    import pdfplumber
    pdf = pdfplumber.open(str(pdf_path))
    rows, sizes, long_sizes = [], Counter(), Counter()
    for pg in pdf.pages:
        pr = []
        for ln in pg.extract_text_lines(return_chars=True, strip=True):
            cs = [c for c in ln["chars"] if c["text"].strip()]
            if not cs:
                continue
            sz = Counter(round(c["size"] * 2) / 2 for c in cs).most_common(1)[0][0]
            fn = Counter(c["fontname"].split("+")[-1] for c in cs).most_common(1)[0][0]
            pr.append((sz, fn, ln["text"].strip()))
            sizes[sz] += len(cs)
            if len(re.sub(r"\s+", "", ln["text"])) >= 25:     # 문단 줄만으로 본문 크기를 추정한다(표 글자가 많은 문서에서 본문 크기를 잘못 잡지 않게)
                long_sizes[sz] += len(cs)
        rows.append(pr)
    pdf.close()
    # 본문 크기: 전체 글자 최다 크기와 긴 줄(25자 이상) 최다 크기 중 큰 쪽.
    # 표가 많은 문서는 표 글자 때문에 앞쪽이 작게 잡히고, 열이 좁은 2단 문서는 긴 줄이 각주뿐이라 뒤쪽이 작게 잡힌다.
    body = max(sizes.most_common(1)[0][0], (long_sizes or sizes).most_common(1)[0][0])
    cands = []
    for pno, pr in enumerate(rows, 1):
        for sz, fn, t in pr:
            n = re.sub(r"\s+", "", t)
            big = sz >= body + 0.9
            boldish = sz >= body + 0.4 and re.search(r"Bold|Black|Heavy|EB|Medium", fn) and len(n) <= 40
            if not (big or boldish) or len(n) < 2 or len(n) > 70 or re.fullmatch(r"[\d\W]+", n):
                continue
            if BULLET.match(t) and not re.match(r"^\d+[.)]\s|^[가-힣][.)]\s", t) or NOT_HEAD.match(t) or t.endswith("다."):
                continue
            cands.append((pno, sz, n))
    # 쪽마다 반복되는 머리글·꼬리말 제외. 같은 글이라도 글꼴 크기가 다르면 다른 것으로 센다(목차 줄과 본문 제목이 같은 글이어도 본문 제목을 살리기 위해)
    fkey = lambda sz, n: (sz, re.sub(r"[\d｢｣「」\[\]]+", "", n))
    freq = Counter(fkey(sz, n) for _, sz, n in cands)
    cands = [c for c in cands if freq[fkey(c[1], c[2])] < 3]
    size_pages, size_count = {}, Counter()
    for pno, sz, _ in cands:
        size_pages.setdefault(sz, set()).add(pno)
        size_count[sz] += 1
    # 같은 크기 후보가 2개 이상인 크기만 제목 단계로 쓴다. 단, 긴 문서에서 앞 3쪽에만 나오는 크기(표지·속표지의 큰 글자)는 뺀다
    ok = lambda sz: size_count[sz] >= 2 and not (len(rows) >= 10 and size_pages[sz] <= {1, 2, 3})
    levels = {sz: i + 1 for i, sz in enumerate(sorted([s for s in size_count if ok(s)], reverse=True)[:3])}
    out = {}
    for pno, sz, n in cands:
        if sz in levels:
            out.setdefault(pno, {})[n] = levels[sz]
            # PDF는 '15 부모님이…'처럼 번호와 제목 첫 줄을 한 줄로 읽지만 추출 블록에서는 번호가 따로 있다 -> 번호를 뗀 글도 같은 제목으로 등록
            m = re.match(r"^\d{1,3}(?=\D)(.+)$", n)
            if m and len(m.group(1)) >= 3:
                out[pno].setdefault(m.group(1), levels[sz])
    return out


def para_split(text):
    """줄 목록을 불릿·번호로 시작하는 줄에서 문단으로 묶는다."""
    paras, cur = [], []
    for ln in text.split(NL):
        if not ln.strip():
            continue
        if (BULLET.match(ln.strip()) or CAP.match(ln.strip()) or NOTE.match(ln.strip())) and cur:
            paras.append(NL.join(cur))
            cur = []
        cur.append(ln.strip())
    if cur:
        paras.append(NL.join(cur))
    return paras


TOPIC = re.compile(r"^[■□]\s*\(([^)]{2,24})\)")


def mk_para(text, page):
    """문단 단위. '■ (연체율 현황) …'처럼 괄호 라벨로 시작하는 요약 문단은 라벨을 소제목(topic)으로 기록한다(제목 글꼴이 없는 요약부용)."""
    u = dict(kind="para", text=text, page=page)
    m = TOPIC.match(text)
    if m:
        u["topic"] = m.group(1).strip()
    return u


def guide_units(doc, pages, heads):
    units = []   # dict(kind, text, page, level)
    for rec in pages:
        hp = heads.get(rec["page"], {})
        for b in rec["blocks"]:
            if b["type"] == "table":
                units.append(dict(kind="table", block=b, page=rec["page"]))
                continue
            t = block_text(b)
            if not t:
                continue
            if b["type"] in ("image_text", "chart_text"):
                units.append(dict(kind="figure", text=t, page=rec["page"], image=b.get("image")))
                continue
            lines, info = [x.strip() for x in t.split(NL) if x.strip()], []
            for s in lines:
                md = re.match(r"^(#{1,3})\s+(.*)", s)
                n = re.sub(r"\s+", "", s)
                lvl = len(md.group(1)) if md else hp.get(n)
                if lvl and not md and len(n) < 4:
                    lvl = None
                info.append((md, lvl))
            buf = []
            for i, s in enumerate(lines):
                md, lvl = info[i]
                if re.fullmatch(r"\d{1,3}", s) and i + 1 < len(lines) and info[i + 1][1]:
                    continue                      # 질문 번호 줄: 바로 다음 줄이 제목이면 그 제목에 딸린 번호이므로 앞 청크 본문에 넣지 않는다
                if lvl:
                    if buf:
                        units.extend(mk_para(p, rec["page"]) for p in para_split(NL.join(buf)))
                        buf = []
                    units.append(dict(kind="head", text=(md.group(2) if md else s), level=lvl, page=rec["page"]))
                else:
                    buf.append(s)
            if buf:
                units.extend(mk_para(p, rec["page"]) for p in para_split(NL.join(buf)))
    # 줄바꿈으로 갈라진 같은 단계 제목은 하나로 합친다
    merged = []
    for u in units:
        if merged and u["kind"] == "head" and merged[-1]["kind"] == "head" and merged[-1]["level"] == u["level"] and merged[-1]["page"] == u["page"]:
            merged[-1]["text"] += " " + u["text"]
        else:
            merged.append(u)
    # 표·그림 바로 앞의 캡션([표 N], (단위: ..))과 바로 뒤의 각주(자료:, 주:)는 그 표·그림 청크에 붙인다
    res = []
    for u in merged:
        if u["kind"] in ("table", "figure"):
            pre = []
            while res and res[-1]["kind"] == "para" and CAP.match(res[-1]["text"]) and res[-1]["page"] == u["page"]:
                pre.insert(0, res.pop()["text"])
            u["pre"] = pre
        elif u["kind"] == "para" and res and res[-1]["kind"] in ("table", "figure") and NOTE.match(u["text"]) and res[-1]["page"] == u["page"]:
            res[-1].setdefault("post", []).append(u["text"])
            continue
        res.append(u)
    return res


def chunk_guide(doc, pages, max_chars=1200, min_chars=150, context_prefix=False, cross_page=False, short_min=100, drop_below=30):
    """cross_page=False: 청크가 쪽 경계를 넘지 않는다(출처를 파일·쪽 하나로 정확히 달기 위해). short_min 미만 청크는 같은 쪽 이웃에 합치고,
    합친 뒤에도 drop_below 미만인 본문 청크는 버린다."""
    title = doc_title(doc, pages)
    pdf_path = DATA / doc["domain"] / doc["file"]
    heads = heading_map(pdf_path)
    units = guide_units(doc, pages, heads)
    chunks, buf, stack = [], dict(items=[], pages=set(), section=""), []

    def path():
        return " > ".join(t for _, t in stack)

    def flush(kind="body"):
        if buf["items"]:
            text = NL.join(buf["items"])
            for k, piece in enumerate(hard_split(text, max_chars + 300)):
                chunks.append(dict(file=doc["file"], page=min(buf["pages"]), pages=sorted(buf["pages"]), text_raw=piece, doc_title=title,
                                   group="guide", section=buf["section"], kind=kind))
        buf["items"], buf["pages"], buf["section"] = [], set(), ""

    def page_break(page):
        return (not cross_page) and buf["pages"] and page not in buf["pages"]

    def add(text, page):
        if page_break(page):
            flush()
        if not buf["items"]:
            buf["section"] = path()
        elif sum(len(x) + 1 for x in buf["items"]) + len(text) > max_chars:
            flush()
            buf["section"] = path()
        buf["items"].append(text)
        buf["pages"].add(page)

    for u in units:
        if u["kind"] == "head":
            if sum(len(x) + 1 for x in buf["items"]) >= min_chars or page_break(u["page"]):
                flush()
            while stack and stack[-1][0] >= u["level"]:
                stack.pop()
            stack.append((u["level"], u["text"]))
            if not buf["items"]:
                buf["section"] = path()
            buf["items"].append(u["text"])
            buf["pages"].add(u["page"])
        elif u["kind"] == "table":
            flush()
            for piece in split_table(u["block"], max_chars, u.get("pre", ()), u.get("post", ())):
                chunks.append(dict(file=doc["file"], page=u["page"], pages=[u["page"]], text_raw=piece, doc_title=title,
                                   group="guide", section=path(), kind="table"))
        elif u["kind"] == "figure":
            flush()
            text = NL.join(list(u.get("pre", [])) + [u["text"]] + list(u.get("post", [])))
            chunks.append(dict(file=doc["file"], page=u["page"], pages=[u["page"]], text_raw=text, doc_title=title,
                               group="guide", section=path(), kind="figure", image=u.get("image")))
        else:
            if u.get("topic"):                    # 요약부 소제목('■ (라벨)'): 제목 4단계처럼 다룬다
                if sum(len(x) + 1 for x in buf["items"]) >= min_chars or page_break(u["page"]):
                    flush()
                while stack and stack[-1][0] >= 4:
                    stack.pop()
                stack.append((4, "(" + u["topic"] + ")"))
            add(u["text"], u["page"])
    flush()
    chunks = merge_short(chunks, short_min)
    chunks = [c for c in chunks if c["kind"] != "body" or len(c["text_raw"].strip()) >= drop_below]
    return finish(chunks, doc, title, context_prefix, "structured")


# ------------------------------------------------------------------ 진입점
def build_chunks(strategy, out_path=None, files=None, size=500, overlap=50, max_chars=1200, min_chars=150, context_prefix=False,
                 cross_page=False):
    """strategy: 'recursive' | 'structured'. 반환: 청크 목록(out_path가 있으면 JSONL로도 저장)."""
    allc = []
    for doc in list_docs():
        if files and not any(f in doc["file"] for f in files):
            continue
        pages = load_pages(doc)
        if strategy == "recursive":
            allc += chunk_recursive(doc, pages, size, overlap, context_prefix)
        elif doc["domain"] == "glossary":
            allc += chunk_glossary(doc, pages, context_prefix)
        elif doc["domain"] == "standard_terms":
            allc += chunk_terms(doc, pages, max_chars, context_prefix)
        else:
            allc += chunk_guide(doc, pages, max_chars, min_chars, context_prefix, cross_page)
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            for c in allc:
                f.write(json.dumps(c, ensure_ascii=False) + NL)
    return allc


def summarize(chunks):
    import statistics
    lens = [len(c["text_raw"]) for c in chunks]
    by = Counter(c["group"] for c in chunks)
    print(f"청크 {len(chunks)}개 | 글자 수 중앙값 {statistics.median(lens):.0f}, 평균 {sum(lens)/len(lens):.0f}, 최소 {min(lens)}, 최대 {max(lens)}")
    print("분야별:", dict(by), "| 종류별:", dict(Counter(c["kind"] for c in chunks)))
    print("200자 미만:", sum(l < 200 for l in lens), "| 1500자 초과:", sum(l > 1500 for l in lens))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("strategy", choices=["recursive", "structured"])
    ap.add_argument("--out")
    ap.add_argument("--size", type=int, default=500)
    ap.add_argument("--overlap", type=int, default=50)
    ap.add_argument("--max-chars", type=int, default=1200)
    ap.add_argument("--prefix", action="store_true")
    ap.add_argument("--cross-page", action="store_true", help="구조형 가이드 청크가 쪽 경계를 넘도록 허용")
    a = ap.parse_args()
    cs = build_chunks(a.strategy, a.out, size=a.size, overlap=a.overlap, max_chars=a.max_chars, context_prefix=a.prefix,
                      cross_page=a.cross_page)
    summarize(cs)
