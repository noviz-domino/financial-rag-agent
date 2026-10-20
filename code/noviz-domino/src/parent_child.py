"""Parent-Child + 이웃·참조 링크 청킹 (docs/청크_구조_선택지.md 의 B + D).

parent (LLM 에 전달할 문맥)            child (임베딩·검색 단위)
  용어집  용어 1개                      = parent 와 같은 텍스트 1개
  약관    조항 전체 (제17조)            항(①②…) 단위, 짧은 항은 child_max 까지 묶음
  가이드북·추가 반쪽 페이지 영역        섹션 텍스트 / 표(행 단위 분할, 머리글 반복) / 그림

링크
  prev_id / next_id : 같은 문서 안 앞뒤 child (parent 끼리도 따로)
  refs              : 본문의 '그림 Ⅲ-5', '참고 1-3', '[표 2]' → 해당 그림·표 child
                      약관의 '제17조에 따라' → 같은 약관의 제17조 parent  (「법」·시행령 조항은 제외)
  ref_by            : 역방향 (그림 child 가 어느 본문에서 언급되는지)

출력
  data/chunks/parentchild_{child_max}.jsonl          child (check_chunks·인덱싱 대상)
  data/chunks/parents/parentchild_{child_max}.jsonl  parent (id → 문맥 조회용 저장소)
"""
import json
import re
from collections import defaultdict

from src.chunking import (OUT, P, ROOT, covered_by_figure, doc_title, figure_text, load_jsonl, make_chunk,
                          page_sid, sections_of,
                          slug, split_long)

FIG_LABEL = re.compile(r"(그림|참고|표)\s*\]?\s*([IVXⅠ-Ⅹ]+\s*[-.]\s*\d+|\d+\s*-\s*\d+|\d+)")
ART_REF = re.compile(r"제(\d+)조(?:의\d+)?")
EXTERNAL_HINT = re.compile(r"(법|령|규칙|규정|」|시행)\s*(같은 법\s*)?$")
ROMAN = {"Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "Ⅳ": "IV", "Ⅴ": "V", "Ⅵ": "VI", "Ⅶ": "VII", "Ⅷ": "VIII", "Ⅸ": "IX", "Ⅹ": "X"}


def label_key(kind, num):
    num = re.sub(r"\s+", "", num).replace(".", "-")
    for k, v in ROMAN.items():
        num = num.replace(k, v)
    return f"{kind}{num}"


def labels_in(text):
    return {label_key(m.group(1), m.group(2)) for m in FIG_LABEL.finditer(text or "")}


def internal_article_refs(text, own_art):
    """같은 약관 안의 조항 번호만. 줄 첫머리의 '제N조(' 는 조항 제목이므로 제외."""
    out = set()
    for m in ART_REF.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        if m.start() == line_start and text[m.end():m.end() + 1] == "(":
            continue
        if EXTERNAL_HINT.search(text[max(0, m.start() - 14):m.start()]):
            continue
        n = int(m.group(1))
        if n != own_art:
            out.add(n)
    return out


def split_table(caption, md, max_chars):
    """긴 Markdown 표를 행 단위로 나누고, 조각마다 캡션·머리글을 반복한다."""
    lines = md.split("\n")
    head, rows = lines[:2], lines[2:]
    prefix = f"[표] {caption}\n" + "\n".join(head)
    parts, cur = [], []
    for r in rows:
        if cur and len(prefix) + sum(len(x) + 1 for x in cur) + len(r) > max_chars:
            parts.append(cur)
            cur = []
        cur.append(r)
    if cur or not parts:
        parts.append(cur)
    return [prefix + ("\n" + "\n".join(p) if p else "") for p in parts]


def build(child_max=400, add_title=False):
    from src import chunking   # --pages 로 바뀐 입력 파일·이름 접미어를 실행 시점에 읽음
    name = f"parentchild_{child_max}" + ("_title" if add_title else "") + chunking.NAME_SUFFIX
    parents, children = [], []

    def add_parent(file, doc_type, pages, sid, title, text, pid, **meta):
        parents.append(make_chunk(name, file, doc_type, pages, [sid], title, text, pid, False,
                                  level="parent", child_ids=[], **meta))
        return parents[-1]

    def add_child(parent, title, text, idx, section_path, **meta):
        cid = f"{parent['chunk_id']}__c{idx}"
        c = make_chunk(name, parent["file"], parent["doc_type"], parent["pages"], parent["source_ids"], title, text,
                       cid, add_title, level="child", parent_id=parent["chunk_id"], chunk_index=idx,
                       section_path=section_path, half=parent["half"], printed_page=parent["printed_page"], **meta)
        parent["child_ids"].append(cid)
        children.append(c)
        return c

    # ---------------- 용어집 · 약관
    for u in load_jsonl(P / "units.jsonl"):
        if u["doc_type"] == "glossary":
            dic = u["source_id"].rsplit("-", 1)[1]
            p = add_parent(u["file"], "glossary", u["pages"], u["source_id"], u["title"], u["text"],
                           f"{name}__glossary__{dic}")
            add_child(p, u["title"], u["text"], 0, [u["title"]])
        elif u["doc_type"] == "terms":
            art_title = u["title"].split(" ", 1)[1] if " " in u["title"] else u["title"]
            art = int(u["source_id"].rsplit("-", 1)[1])
            p = add_parent(u["file"], "terms", u["pages"], u["source_id"], art_title, u["text"],
                           f"{name}__{slug(u['file'])}__a{art:03d}", article_no=art)
            p["_art_refs"] = internal_article_refs(u["text"], art)
            for i, part in enumerate(split_long(u["text"], child_max)):
                para = re.match(r"\s*([①-⑳])", part)
                text = part if i == 0 else f"{art_title} (계속)\n{part}"
                add_child(p, art_title, text, i, [art_title] + ([para.group(1)] if para else []))

    # ---------------- 가이드북 · 추가 PDF
    figs = defaultdict(list)
    for f in load_jsonl(ROOT / "data" / "figures" / "figures.jsonl"):
        if not f["decorative"]:
            figs[(f["file"], f["page"], f["half"])].append(f)

    for r in load_jsonl(P / chunking.PAGES_FILE):
        if r["doc_type"] not in ("life_guide", "additional"):
            continue
        sid = page_sid(r["file"], r["page"])
        tag = f"p{r['page']}{r['half'] or ''}"
        region_figs = [(f, *figure_text(f)) for f in figs.get((r["file"], r["page"], r["half"]), [])]
        # Gemini 그림이 덮는 표는 빼서 중복을 없앤다 (그림 쪽이 더 정확: 열 누락·행 쪼개짐 없음)
        region_fig_rows = figs.get((r["file"], r["page"], r["half"]), [])
        clean_tables = [t for t in r["tables"] if not t["lossy"] and not covered_by_figure(t, region_fig_rows)]
        # parent = 영역 전체 (본문 + 깨끗한 표 + 그림 텍스트) → LLM 이 그림을 주변 설명과 함께 봄
        parent_text = "\n\n".join([r["text"]] + [f"[표] {t['caption']}\n{t['markdown']}" for t in clean_tables]
                                  + [txt for _, txt, _ in region_figs if txt])
        sections = sections_of(r["text"])
        region_title = (sections[0].split("\n", 1)[0].strip()[:40] if sections else "")
        p = add_parent(r["file"], r["doc_type"], [r["page"]], sid, region_title, parent_text,
                       f"{name}__{slug(r['file'])}__{tag}", half=r["half"], printed_page=r["printed_page"])
        idx = 0
        groups, cur = [], ""
        for sec in sections:
            if cur and len(cur) + len(sec) > child_max:
                groups.append(cur)
                cur = ""
            cur += ("\n" if cur else "") + sec
        if cur:
            groups.append(cur)
        for g in (q for grp in groups for q in split_long(grp, child_max, re.compile(r"(?=\n)"))):
            head = g.strip().split("\n", 1)[0][:40]
            add_child(p, head, g, idx, [x for x in (region_title, head) if x], content_type="text")
            idx += 1
        for t in clean_tables:
            for part in split_table(t["caption"], t["markdown"], max(child_max, 600)):
                add_child(p, t["caption"][:40] or "표", part, idx, [region_title, t["caption"][:40]],
                          content_type="table", source="pdf_table", caption=t["caption"])
                idx += 1
        for f, txt, src in region_figs:
            if not txt:
                continue
            add_child(p, f["caption"][:40] or "그림", txt, idx, [region_title, f["caption"][:40]],
                      content_type="figure", figure_kind=f["kind"], source=src, bbox=f["bbox"],
                      image_path=f["image_path"], caption=f["caption"], num_check=f["num_check"])
            idx += 1

    link(parents, children)
    for p in parents:
        p.pop("_art_refs", None)
    return name, parents, children


def link(parents, children):
    # 이웃: 같은 문서 안에서 생성 순서 = 문서 순서
    for group in (parents, children):
        by_file = defaultdict(list)
        for c in group:
            by_file[c["file"]].append(c)
        for seq in by_file.values():
            for a, b in zip(seq, seq[1:]):
                a["next_id"], b["prev_id"] = b["chunk_id"], a["chunk_id"]
            seq[0].setdefault("prev_id", None)
            seq[-1].setdefault("next_id", None)
    for c in parents + children:
        c.setdefault("prev_id", None)
        c.setdefault("next_id", None)
        c.setdefault("refs", [])
        c.setdefault("ref_by", [])

    # 그림·표 참조: 같은 문서 안에서 캡션(또는 그림 텍스트 앞부분)의 라벨 → 대상 child
    # 대상 라벨은 '캡션' 또는 '라벨로 시작하는 첫 줄'에서만 읽는다.
    # (그림 본문 앞부분까지 보면 크롭에 함께 잡힌 옆 표의 제목을 자기 라벨로 오인함)
    targets = defaultdict(dict)
    for c in children:
        if c["content_type"] in ("figure", "table"):
            body = re.sub(r"^\[(그림|표)\]\s*", "", c["text"]).split("\n")
            first = next((ln for ln in body[1:3] if FIG_LABEL.match(ln.strip(" [<"))), "")
            for key in labels_in((c.get("caption") or "") + " " + first):
                targets[c["file"]].setdefault(key, c["chunk_id"])
    by_id = {c["chunk_id"]: c for c in parents + children}
    for c in children:
        if c["content_type"] != "text":
            continue
        for key in sorted(labels_in(c["text"])):
            tid = targets[c["file"]].get(key)
            if tid and tid != c["chunk_id"]:
                c["refs"].append({"type": "figure", "label": key, "target": tid})
                by_id[tid]["ref_by"].append(c["chunk_id"])

    # 약관 조항 참조: child 는 소속 조항(parent)이 언급한 다른 조항 parent 를 가리킴
    art_parent = {(p["file"], p.get("article_no")): p["chunk_id"] for p in parents if p.get("article_no")}
    for p in parents:
        for n in sorted(p.get("_art_refs", ())):
            tid = art_parent.get((p["file"], n))
            if tid:
                p["refs"].append({"type": "article", "label": f"제{n}조", "target": tid})
                by_id[tid]["ref_by"].append(p["chunk_id"])
    for c in children:
        c["refs"] += [r for r in by_id[c["parent_id"]]["refs"] if r["type"] == "article"] if c.get("parent_id") else []


def save(name, parents, children):
    (OUT / "parents").mkdir(parents=True, exist_ok=True)
    for path, rows in ((OUT / f"{name}.jsonl", children), (OUT / "parents" / f"{name}.jsonl", parents)):
        ids = [r["chunk_id"] for r in rows]
        assert len(ids) == len(set(ids)), f"chunk_id 중복: {path}"
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{name}: parents {len(parents)} / children {len(children)} -> {OUT / (name + '.jsonl')}")
