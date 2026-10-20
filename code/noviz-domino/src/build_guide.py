"""팀원용 설명서(docs/파이프라인_안내서.html)를 만든다.

docs/guide/guide_template.html 의 {{…}} 자리에 최신 검증 수치와 72쪽 그림을 채워 넣는다.
실행 순서: python -m src.verify_all  →  python -m src.build_guide
"""
import base64
import io
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
tpl = (ROOT / "docs/guide/guide_template.html").read_text(encoding="utf-8")


def load_jsonl(p):
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


# 그림 요약
figs = load_jsonl(ROOT / "data/figures/figures.jsonl")
t = [f for f in figs if not f["decorative"] and not f["dup_of"]]
done = [f for f in t if f.get("md")]
nc = [f["num_check"] for f in done if f.get("num_check") is not None]
warn = sum(1 for f in done if f.get("unverified_numbers"))
skip = Counter(f["skip_reason"] for f in figs if f.get("skip_reason"))
fig_summary = (f"그림 후보 {len(figs)}개 중 장식·중복을 빼고 <b>{len(t)}개</b>만 Gemini(gemini-2.5-flash)로 보냈어요"
               f" (반복 배너 {skip.get('repeated_image', 0)}개, 글자 얹힌 배경 {skip.get('text_over_background', 0)}개, QR·로고 {skip.get('small_no_text', 0)}개 제외)."
               f" 변환 {len(done)}개 · 오류 {sum(1 for f in t if f.get('vlm_error'))}개."
               f" 숫자 검증 0.9 이상 {sum(1 for x in nc if x >= .9)}개, 0.5 미만 {sum(1 for x in nc if x < .5)}개,"
               f" 원문에 없는 숫자 경고가 붙은 그림 {warn}개. 전체 비용은 입력 약 23만·출력 약 49만 토큰이었어요.")

# 72쪽 그림 (작게 줄여서 내장)
p72 = next(f for f in figs if f["file"] == "housing_pension_2026.pdf" and f["page"] == 72 and f["kind"] == "vector_chart")
im = Image.open(ROOT / p72["image_path"]).convert("RGB")
im.thumbnail((900, 1200))
buf = io.BytesIO()
im.save(buf, format="JPEG", quality=82)
p72_img = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

# 검증 결과 문서에서 표 가져오기
ver = (ROOT / "docs/검증_결과.md").read_text(encoding="utf-8")


def md_table_to_html(md_lines, highlight=None):
    rows = [l.strip().strip("|").split("|") for l in md_lines if l.strip().startswith("|")]
    head, body = rows[0], rows[2:]
    numcol = [i for i, h in enumerate(head) if i > 0]
    h = "<tr>" + "".join(f"<th{' class=num' if i in numcol else ''}>{c.strip()}</th>" for i, c in enumerate(head)) + "</tr>"
    trs = []
    for r in body:
        cls = " class=best" if highlight and highlight(r) else ""
        trs.append(f"<tr{cls}>" + "".join(f"<td{' class=num' if i in numcol else ''}>{c.strip()}</td>" for i, c in enumerate(r)) + "</tr>")
    return '<div class="tbl"><table>' + h + "".join(trs) + "</table></div>"


def section(title):
    m = re.search(rf"## {re.escape(title)}.*?\n(.*?)(?=\n## |\Z)", ver, flags=re.S)
    return m.group(1) if m else ""


gold_md = [l for l in section("1. 정답 근거 연결").split("\n") if l.startswith("|")]
gold_table = "<h3>정답 위치 연결 확인</h3>" + md_table_to_html(gold_md)
bm_md = [l for l in section("6. 검색 비교").split("\n") if l.startswith("|")]
bm25_table = ("<h3>청킹 전략별 검색 성능 (BM25)</h3>" + md_table_to_html(
    bm_md, highlight=lambda r: r[0].strip() == "parentchild_400 [parent]") if len(bm_md) > 2 else
    "<p><i>검색 비교 표가 아직 없어요. <code>python -m src.verify_all</code>(--quick 없이)을 끝까지 돌린 뒤 다시 만들어 주세요.</i></p>")
checks = []
for title in ("3. 그림 변환", "4. 링크", "5. 중복 제거"):
    for l in section(title).split("\n"):
        if l.startswith("- "):
            checks.append("<li>" + re.sub(r"`([^`]+)`", r"<code>\1</code>", l[2:]) + "</li>")
check_lines = "<h3>그 밖의 점검</h3><ul class=\"tight\">" + "".join(checks) + "</ul>"

# 로더 비교 (src.compare_p4l → src.loader --merge-p4l → 청크 비교 결과가 있으면)
lc = ROOT / "data/chunks/loader_compare/compare_bm25_loader.json"
loader_table = ""
if lc.exists():
    LOADER = {"": "우리 로더 (v2)", "_p4l_layout": "pymupdf4llm", "_v3": "합친 로더 (v3)"}
    rows = []
    for r in json.loads(lc.read_text(encoding="utf-8")):
        name = r["strategy"].replace("loader_compare/", "")
        suffix = next((s for s in ("_p4l_layout", "_v3") if s in name), "")
        rows.append([name.replace(suffix, ""), LOADER[suffix], r["life_guide_hit@1"], r["life_guide_hit@5"],
                     r["additional_hit@1"], r["additional_hit@5"], r["additional_mrr"]])
    md = ["| 청킹 전략 | 로더 | 가이드 H@1 | 가이드 H@5 | 추가 H@1 | 추가 H@5 | 추가 MRR |", "|---|---|---|---|---|---|---|"]
    md += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    loader_table = ("<h3>로더만 바꿔서 잰 검색 점수 (BM25, 청킹 규칙은 동일)</h3>"
                    + md_table_to_html(md, highlight=lambda r: "v3" in r[1])
                    + "<p>가이드북 20문항(1문항 = 0.05), 추가 PDF 60문항(1문항 ≈ 0.017)이에요. "
                      "BM25는 단어 순서를 보지 않아서 읽는 순서 개선 효과가 잘 안 드러나요. 차이가 1~2문항이면 '비슷하다'로 읽어 주세요.</p>")


def count_lossy(name):
    p = ROOT / "data/processed" / name
    return sum(t["lossy"] for r in load_jsonl(p) for t in r["tables"]) if p.exists() else "?"


out = (tpl.replace("{{LOADER_TABLE}}", loader_table)
       .replace("{{LOSSY_V2}}", str(count_lossy("pages_v2.jsonl"))).replace("{{LOSSY_V3}}", str(count_lossy("pages_v3.jsonl")))
       .replace("{{FIG_SUMMARY}}", fig_summary).replace("{{P72_IMG}}", p72_img)
       .replace("{{GOLD_TABLE}}", gold_table).replace("{{BM25_TABLE}}", bm25_table)
       .replace("{{CHECK_LINES}}", check_lines)
       .replace("{{GENERATED}}", f"수치는 {datetime.now():%Y-%m-%d %H:%M} 기준 <code>python -m src.verify_all</code> 결과예요."))
assert "{{" not in out, re.findall(r"\{\{\w+\}\}", out)
(ROOT / "docs/파이프라인_안내서.html").write_text(out, encoding="utf-8")
print("->", ROOT / "docs/파이프라인_안내서.html", len(out) // 1024, "KB")
