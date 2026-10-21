"""로더 품질 점검: 기존 추출(pages.jsonl) vs 레이아웃 로더(pages_v2.jsonl).

1) 문서별 글자 보존율   : 새 로더가 내용을 과하게 버리지 않았는지
2) BM25 페이지 검색 Hit@k: 가이드북·추가 PDF 80문항의 정답 페이지를 찾는지 (LLM 불필요, 비용 0)

실행:  python -m src.check_loader
       python -m src.check_loader pages_p4l_layout.jsonl pages_p4l_legacy.jsonl   # 다른 로더 결과도 같이 비교
"""
import sys
import json
from collections import defaultdict
from pathlib import Path

from kiwipiepy import Kiwi
from rank_bm25 import BM25Okapi

from src.loader import render_text

ROOT = Path(__file__).resolve().parent.parent
P = ROOT / "data" / "processed"
kiwi = Kiwi()


def tok(text):
    return [t.form for t in kiwi.tokenize(text) if t.tag[0] in "NVSX" or t.tag in ("SL", "SN")]


def load_jsonl(p):
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def page_key(file, page):
    stem = "fss-life-guide" if file == "financial_life_guide.pdf" else file[:-4]
    return f"{stem}-pdf-{page}"


def build_docs(version):
    """페이지 단위 문서로 합침 (정답 ID가 페이지 단위이므로 펼침면 L/R도 다시 합쳐서 비교)."""
    docs = defaultdict(str)
    if version.endswith(".jsonl"):   # 다른 로더 결과 (pages_v2 와 같은 필드)
        for r in load_jsonl(P / version):
            if r["doc_type"] in ("life_guide", "additional"):
                docs[page_key(r["file"], r["page"])] += render_text(r) + "\n"
    elif version == "old":
        for r in load_jsonl(P / "pages.jsonl"):
            if r["doc_type"] in ("life_guide", "additional"):
                docs[page_key(r["file"], r["page"])] += r["text"] + "\n"
    else:
        for r in load_jsonl(P / "pages_v2.jsonl"):
            if r["doc_type"] in ("life_guide", "additional"):
                docs[page_key(r["file"], r["page"])] += render_text(r) + "\n"
    return docs


def main():
    cases = load_jsonl(ROOT / "03_answers/positive_life_guide_cases.jsonl") + \
        load_jsonl(ROOT / "03_answers/additional_cases.jsonl")
    old, new = build_docs("old"), build_docs("new")
    extra = {f.removeprefix("pages_").removesuffix(".jsonl"): build_docs(f) for f in sys.argv[1:]}

    print("== 1) 문서별 글자 수 (old → new, 비율) ==")
    names = ["new"] + list(extra)
    vers = [new] + list(extra.values())
    by_file = defaultdict(lambda: [0] * (1 + len(vers)))
    for k in old:
        f = k.rsplit("-pdf-", 1)[0]
        by_file[f][0] += len(old[k].replace(" ", ""))
        for j, v in enumerate(vers, 1):
            by_file[f][j] += len(v.get(k, "").replace(" ", ""))
    print(f"  {'':40s} {'old':>7s}  " + "  ".join(f"{n:>18s}" for n in names))
    for f, (a, *bs) in sorted(by_file.items()):
        print(f"  {f:40s} {a:7d}  " + "  ".join(f"{b:9d} ({b / a:6.0%})" for b in bs))

    print("\n== 2) BM25(Kiwi) 페이지 검색 — 정답 페이지 Hit@k ==")
    for name, docs in (("old", old), ("new", new), *extra.items()):
        keys = sorted(docs)
        bm25 = BM25Okapi([tok(docs[k]) for k in keys])
        res = {1: 0, 3: 0, 5: 0}
        for c in cases:
            scores = bm25.get_scores(tok(c["question"]))
            ranked = [keys[i] for i in sorted(range(len(keys)), key=lambda i: -scores[i])[:5]]
            gold = set(c["expected_source_ids"])
            for k in res:
                res[k] += bool(gold & set(ranked[:k]))
        print(f"  {name}: " + "  ".join(f"Hit@{k}={v / len(cases):.3f}" for k, v in res.items()) + f"  (n={len(cases)})")


if __name__ == "__main__":
    main()
