"""구조형 청킹 (기획서 3-4). 분할 규칙은 jjh chunking.py, 청크 필드·parent 구조는 noviz 쪽.

  용어집   용어 1개 = 청크 1개
  약관     조항 1개 = 청크 1개. 길면 ①②항 경계에서만 나누고 조 제목을 반복, 제목만 있는 조각은 만들지 않음
  가이드북·추가  글꼴로 찾은 제목 단위. 100자 미만 짧은 절은 같은 쪽 이웃과 합침. 섹션 경로(문서 > 장 > 절)
  표·그림  본문과 따로 청크 (긴 표는 행 단위로 나누고 머리글 반복)
같은 코드가 max_chars 만 줄여 parent-child 의 child(400자) 도 만든다 (build.py).
각 청크는 parent_id(조항 전체·반쪽 페이지)를 가리키고, parent 는 별도 레코드로 저장한다.
"""
from __future__ import annotations

import re

from src.chunking.common import doc_title, make_chunk, page_source_id, slug
from src.chunking.headings import BULLET, heading_map, norm
from src.tables.extract import split_table_md

CLAUSE = re.compile(r"^[①-⑳]")
CHAPTER = re.compile(r"^제\d+장\s")
CAP = re.compile(r"^(?:\[(?:표|그림)|\((?:단위|기준)|<(?:표|그림))")
NOTE = re.compile(r"^(?:자료\s*[:：]|주\s*[:：)]|출처\s*[:：]|<출처|※)")
SEC_LINE = re.compile(r"^\[세로\]")


# ------------------------------------------------------------------ 용어집
def chunk_glossary(units, variant):
    chunks, parents = [], []
    for u in units:
        dic = u["source_id"].rsplit("-", 1)[1]
        pid = f"P::glossary::{dic}"
        parents.append({"chunk_id": pid, "text": u["text"], "file": u["file"], "doc_type": "glossary",
                        "pages": u["pages"], "half": None, "section": f"{doc_title(u['file'])} > {u['title']}",
                        "child_ids": []})
        c = make_chunk(f"{variant}__glossary__{dic}", u["file"], "glossary", u["pages"],
                       f"{doc_title(u['file'])} > {u['title']}", u["text"], source_ids=[u["source_id"]], parent_id=pid,
                       strategy=variant, title=u["title"])
        parents[-1]["child_ids"].append(c["chunk_id"])
        chunks.append(c)
    return chunks, parents


# ------------------------------------------------------------------ 약관
def split_article(text: str, label: str, max_chars: int) -> list[str]:
    """조항 본문을 ①②항 경계에서만 나눈다. 둘째 조각부터 조 제목(label)을 앞에 반복한다.
    제목 줄(+항 앞 도입부)은 첫 항과 함께 두어 '제목만 있는 조각'을 만들지 않는다."""
    if len(text) <= max_chars:
        return [text]
    lines = text.split("\n")
    groups, cur = [], []
    for ln in lines:
        # 새 항이 시작될 때 이전 묶음에 이미 항이 있으면 끊는다 → 첫 묶음 = 조 제목 + 도입부 + 1항 (제목만 있는 조각 없음)
        if CLAUSE.match(ln.strip()) and any(CLAUSE.match(x.strip()) for x in cur):
            groups.append(cur)
            cur = []
        cur.append(ln)
    if cur:
        groups.append(cur)
    # 항이 하나도 없으면(번호 목록 등) 줄 단위로 나눈다. 항 하나가 길어도 항 경계 밖에서는 자르지 않는다.
    if len(groups) == 1:
        groups = [[ln] for ln in lines] if not any(CLAUSE.match(x.strip()) for x in lines) else groups
    # 도입부(제목 줄 등 첫 항 앞 글)를 첫 항과 한 묶음으로
    if len(groups) > 1 and not CLAUSE.match(groups[0][0].strip()) and not any(CLAUSE.match(x.strip()) for x in groups[0]):
        groups[1] = groups[0] + groups[1]
        groups.pop(0)
    pieces, cur_p, cur_len = [], [], 0
    for g in groups:
        glen = sum(len(x) + 1 for x in g)
        if cur_p and cur_len + glen > max_chars:
            pieces.append(cur_p)
            cur_p, cur_len = [], 0
        cur_p += g
        cur_len += glen
    if cur_p:
        pieces.append(cur_p)
    out = []
    for i, p in enumerate(pieces):
        body = "\n".join(p)
        out.append(body if i == 0 else f"{label}\n{body}")
    return out


def chunk_terms(units, variant, max_chars):
    chunks, parents = [], []
    by_file = {}
    for u in units:
        by_file.setdefault(u["file"], []).append(u)
    for file, us in by_file.items():
        chapter = ""
        title = doc_title(file)
        for k, u in enumerate(us):
            lines = u["text"].split("\n")
            kept, next_chapter = [], None
            for ln in lines:
                if CHAPTER.match(ln.strip()):      # 장 제목 줄은 본문이 아니라 다음 조항들의 섹션 경로
                    next_chapter = ln.strip()
                else:
                    kept.append(ln)
            text = "\n".join(kept).strip()
            if not text:
                chapter = next_chapter or chapter
                continue
            section = " > ".join(x for x in (title, chapter, u["title"]) if x)
            pid = f"P::{slug(file)}::a{k:03d}"
            parents.append({"chunk_id": pid, "text": text, "file": file, "doc_type": "terms", "pages": u["pages"],
                            "half": None, "section": section, "child_ids": [], "article_no": u["article_no"],
                            "article_sub": u["article_sub"]})
            for i, piece in enumerate(split_article(text, u["title"], max_chars)):
                c = make_chunk(f"{variant}__{slug(file)}__a{k:03d}__{i}", file, "terms", u["pages"], section, piece,
                               source_ids=[u["source_id"]], parent_id=pid, chunk_index=i, strategy=variant,
                               title=u["title"], article_no=u["article_no"], article_sub=u["article_sub"])
                parents[-1]["child_ids"].append(c["chunk_id"])
                chunks.append(c)
            chapter = next_chapter or chapter
    return chunks, parents


# ------------------------------------------------------------------ 가이드북·추가
def hard_split(text, max_chars):
    if len(text) <= max_chars:
        return [text]
    out, cur = [], ""
    for ln in text.split("\n"):
        while len(ln) > max_chars:
            if cur:
                out.append(cur)
                cur = ""
            out.append(ln[:max_chars])
            ln = ln[max_chars:]
        if cur and len(cur) + len(ln) + 1 > max_chars:
            out.append(cur)
            cur = ""
        cur = cur + "\n" + ln if cur else ln
    if cur:
        out.append(cur)
    return out


def para_split(lines):
    """줄 목록을 불릿·번호·캡션·각주로 시작하는 줄에서 문단으로 묶는다."""
    paras, cur = [], []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if (BULLET.match(s) or CAP.match(s) or NOTE.match(s)) and cur:
            paras.append("\n".join(cur))
            cur = []
        cur.append(s)
    if cur:
        paras.append("\n".join(cur))
    return paras


def ocr_ok(text: str) -> bool:
    """OCR 글이 쓸 만한가: 한글·영숫자·일반 문장부호 비율 85% 이상 (화면 캡처의 깨진 OCR 은 검색을 어지럽힌다)."""
    t = re.sub(r"\s+", "", text or "")
    if len(t) < 15:
        return False
    good = sum(1 for ch in t if re.match(r"[가-힣A-Za-z0-9.,%()·~:/\-\[\]+]", ch))
    return good / len(t) >= 0.85


def figure_chunk_text(f):
    """그림 청크 본문. 설명(Gemini) > OCR(raster) > 텍스트 레이어(벡터 차트·도식). 반환: (text|None, source, caution)."""
    head = f"[그림] {f['caption']}".strip()
    desc = (f.get("description") or "").strip()
    if desc and not f.get("length_flag") and f.get("gemini_kind") != "decorative":
        warn = ""
        if f.get("caution"):
            warn = "\n※ 원문에 숫자로 적혀 있지 않은 값(인용 주의): " + ", ".join(f["unverified_numbers"][:20])
        return f"{head}\n{desc}{warn}", "gemini", bool(f.get("caution"))
    if f["kind"] == "raster" and ocr_ok(f.get("ocr_text")):
        return f"{head}\n{f['ocr_text']}", "ocr", False
    if f["kind"] in ("vector_chart", "diagram"):
        parts = [f.get("text_layer") or ""]
        if (f.get("ocr_text") or "").strip() and (f.get("hidden_chars") or 0) >= 10:
            parts.append(f"[OCR] {f['ocr_text']}")
        body = "\n".join(p for p in parts if p.strip())
        if body:
            return f"{head}\n{body}", "text_layer", False
    return None, None, False


def covered_by_figure(table_bbox, figs, ratio=0.6):
    """이 표를 설명이 있는 그림 영역이 덮고 있으면 True (같은 내용이 두 번 들어가지 않게 그림 쪽만 남긴다)."""
    tb = table_bbox
    t_area = max(1e-6, (tb[2] - tb[0]) * (tb[3] - tb[1]))
    for f in figs:
        if f.get("decorative") or not f.get("description"):
            continue
        fb = f["bbox"]
        inter = max(0, min(tb[2], fb[2]) - max(tb[0], fb[0])) * max(0, min(tb[3], fb[3]) - max(tb[1], fb[1]))
        if inter >= ratio * t_area:
            return True
    return False


class GuideChunker:
    """가이드북·추가 PDF 한 파일을 제목 단위 청크로. jjh chunk_guide 규칙(짧은 절 합침·쪽 경계·섹션 경로)."""

    def __init__(self, file, doc_type, variant, max_chars, short_min=100, min_chars=150, drop_below=30):
        self.file, self.doc_type, self.variant = file, doc_type, variant
        self.max_chars, self.short_min, self.min_chars, self.drop_below = max_chars, short_min, min_chars, drop_below
        self.title = doc_title(file)
        self.items = []   # 확정된 청크 재료

    # ---- 문서 순서대로 단위 만들기
    def units_of(self, records, heads, figs_by_region):
        units = []
        for r in records:
            page, half = r["page"], r["half"]
            hp = heads.get(page, {})
            lines = [l for l in r["text"].split("\n") if l.strip()]
            buf = []
            for i, s in enumerate(lines):
                s = s.strip()
                lvl = hp.get(norm(s)) if len(norm(s)) >= 4 and not SEC_LINE.match(s) else None
                if re.fullmatch(r"\d{1,3}", s) and i + 1 < len(lines) and hp.get(norm(lines[i + 1])):
                    continue   # 질문 번호 줄: 바로 다음 줄이 제목이면 그 제목에 딸린 번호
                if lvl:
                    units += [dict(kind="para", text=p, page=page, half=half) for p in para_split(buf)]
                    buf = []
                    units.append(dict(kind="head", text=s, level=lvl, page=page, half=half))
                else:
                    buf.append(s)
            units += [dict(kind="para", text=p, page=page, half=half) for p in para_split(buf)]
            region_figs = [f for f in figs_by_region.get((page, half), []) if not f.get("decorative")]
            for t in r["tables"]:
                if t["lossy"] or covered_by_figure(t["bbox"], region_figs):
                    continue
                units.append(dict(kind="table", table=t, page=page, half=half))
            n_fig = 0
            for f in region_figs:
                text, src, caution = figure_chunk_text(f)
                if text:
                    n_fig += 1
                    units.append(dict(kind="figure", text=text, source=src, caution=caution, fig=f, page=page, half=half))
            if r["figure_lines"] and not n_fig:
                units.append(dict(kind="figure_lines", text="[그림·표 수치] " + " / ".join(r["figure_lines"]),
                                  page=page, half=half))
        # 줄바꿈으로 갈라진 같은 단계 제목은 합침
        merged = []
        for u in units:
            if merged and u["kind"] == "head" and merged[-1]["kind"] == "head" and \
                    merged[-1]["level"] == u["level"] and merged[-1]["page"] == u["page"]:
                merged[-1]["text"] += " " + u["text"]
            else:
                merged.append(u)
        return merged

    def run(self, units):
        stack, buf = [], {"items": [], "pages": set(), "half": None, "section": ""}
        out = []

        def path():
            return " > ".join(t for _, t in stack)

        def sec(extra=""):
            return " > ".join(x for x in (self.title, path()) if x)

        def flush():
            if buf["items"]:
                text = "\n".join(buf["items"])
                for piece in hard_split(text, self.max_chars + 300):
                    out.append(dict(kind="body", text=piece, pages=sorted(buf["pages"]), half=buf["half"],
                                    section=buf["section"], content_type="text", source="text_layer"))
            buf.update(items=[], pages=set(), half=None, section="")

        def page_break(p):
            return buf["pages"] and p not in buf["pages"]

        def add(text, page, half):
            if page_break(page):
                flush()
            if not buf["items"]:
                buf["section"], buf["half"] = sec(), half
            elif sum(len(x) + 1 for x in buf["items"]) + len(text) > self.max_chars:
                flush()
                buf["section"], buf["half"] = sec(), half
            buf["items"].append(text)
            buf["pages"].add(page)

        for u in units:
            if u["kind"] == "head":
                if sum(len(x) + 1 for x in buf["items"]) >= self.min_chars or page_break(u["page"]):
                    flush()
                while stack and stack[-1][0] >= u["level"]:
                    stack.pop()
                stack.append((u["level"], u["text"]))
                if not buf["items"]:
                    buf["section"], buf["half"] = sec(), u["half"]
                buf["items"].append(u["text"])
                buf["pages"].add(u["page"])
            elif u["kind"] == "para":
                add(u["text"], u["page"], u["half"])
            elif u["kind"] == "table":
                flush()
                t = u["table"]
                for piece in split_table_md(t["markdown"], max(self.max_chars, 600), t["caption"]):
                    out.append(dict(kind="table", text=piece, pages=[u["page"]], half=u["half"], section=sec(),
                                    content_type="table", source=t.get("source", "pdf_table"),
                                    extra={"caption": t["caption"], "table_id": t.get("table_id"), "bbox": t["bbox"]}))
            elif u["kind"] == "figure":
                flush()
                f = u["fig"]
                out.append(dict(kind="figure", text=u["text"], pages=[u["page"]], half=u["half"], section=sec(),
                                content_type="figure", source=u["source"],
                                extra={"figure_id": f["figure_id"], "figure_kind": f["kind"], "gemini_kind": f.get("gemini_kind"),
                                       "caption": f["caption"], "bbox": f["bbox"], "image_path": f["image_path"],
                                       "num_check": f.get("num_check"), "caution": u["caution"]}))
            else:   # figure_lines
                flush()
                out.append(dict(kind="figure_lines", text=u["text"], pages=[u["page"]], half=u["half"], section=sec(),
                                content_type="figure", source="text_layer", extra={}))
        flush()
        return self.merge_short(out)

    def merge_short(self, items):
        """본문·그림 수치 청크 중 short_min 미만을 같은 쪽 이웃에 합친다 (앞 청크 뒤에, 없으면 뒤 청크 앞에)."""
        res, i = [], 0
        same = lambda a, b: set(a["pages"]) & set(b["pages"])
        while i < len(items):
            c = items[i]
            if c["kind"] in ("body", "figure_lines") and len(c["text"]) < self.short_min:
                if res and same(res[-1], c) and res[-1]["kind"] in ("body", "figure_lines"):
                    res[-1]["text"] += "\n" + c["text"]
                    res[-1]["pages"] = sorted(set(res[-1]["pages"]) | set(c["pages"]))
                    i += 1
                    continue
                if i + 1 < len(items) and same(items[i + 1], c) and items[i + 1]["kind"] in ("body", "figure_lines"):
                    items[i + 1]["text"] = c["text"] + "\n" + items[i + 1]["text"]
                    i += 1
                    continue
            res.append(c)
            i += 1
        return [c for c in res if c["kind"] != "body" or len(c["text"].strip()) >= self.drop_below]


def chunk_guide_file(file, doc_type, records, pdf_path, figs, variant, max_chars, short_min):
    """반환: (chunks, parents). parent = 반쪽 페이지 전체(본문+표+그림 글)."""
    heads = heading_map(pdf_path)
    figs_by_region = {}
    for f in figs:
        figs_by_region.setdefault((f["page"], f["half"]), []).append(f)
    gc = GuideChunker(file, doc_type, variant, max_chars, short_min)
    units = gc.units_of(records, heads, figs_by_region)
    items = gc.run(units)
    # parent: 쪽(반쪽) 레코드 전체
    parents, parent_of = [], {}
    for r in records:
        key = (r["page"], r["half"])
        parts = [r["text"]] + [f"[표] {t['caption']}\n{t['markdown']}" for t in r["tables"] if not t["lossy"]]
        for f in figs_by_region.get(key, []):
            if not f.get("decorative"):
                t, _, _ = figure_chunk_text(f)
                if t:
                    parts.append(t)
        pid = f"P::{slug(file)}::p{r['page']}{r['half'] or ''}"
        parent_of[key] = pid
        parents.append({"chunk_id": pid, "text": "\n\n".join(p for p in parts if p).strip(), "file": file,
                        "doc_type": doc_type, "pages": [r["page"]], "half": r["half"],
                        "section": gc.title, "child_ids": []})
    pmap = {p["chunk_id"]: p for p in parents}
    chunks, counter = [], {}
    for it in items:
        page, half = it["pages"][0], it["half"]
        key = (page, half)
        tag = f"p{page}{half or ''}"
        i = counter.get(tag, 0)
        counter[tag] = i + 1
        pid = parent_of.get(key) or parent_of.get((page, None))
        extra = dict(it.get("extra") or {})
        c = make_chunk(f"{variant}__{slug(file)}__{tag}__{i}", file, doc_type, it["pages"], it["section"], it["text"],
                       content_type=it["content_type"], source=it["source"],
                       source_ids=[page_source_id(file, p) for p in it["pages"]], parent_id=pid, half=half,
                       chunk_index=i, strategy=variant, **extra)
        if pid in pmap:
            pmap[pid]["child_ids"].append(c["chunk_id"])
        chunks.append(c)
    # 정답 근거 누락 0 을 보장: 어떤 청크에도 덮이지 않은 쪽은 쪽 전체 텍스트로 보조 청크를 만든다
    covered = {p for c in chunks for p in c["pages"]}
    for r in records:
        if r["page"] in covered:
            continue
        key = (r["page"], r["half"])
        pid = parent_of[key]
        text = pmap[pid]["text"] or f"[{r['page']}쪽: 추출 가능한 텍스트 없음]"
        c = make_chunk(f"{variant}__{slug(file)}__p{r['page']}{r['half'] or ''}__fb", file, doc_type, [r["page"]],
                       gc.title, text, source="text_layer" if pmap[pid]["text"] else "placeholder",
                       source_ids=[page_source_id(file, r["page"])], parent_id=pid, half=r["half"], strategy=variant,
                       fallback=True)
        pmap[pid]["child_ids"].append(c["chunk_id"])
        chunks.append(c)
        covered.add(r["page"])
    return chunks, parents
