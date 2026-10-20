"""VLM 시험 변환: 그림 종류별로 1개씩 골라 Markdown 변환 결과·숫자 검증·토큰·시간을 보여준다.

figures.jsonl 을 다시 만들지 않고(그림 추출 2분 생략) 이미 잘라 둔 PNG 를 그대로 쓴다.
결과는 캐시(data/cache/vlm_md/)에 남으므로, 나중에 전체 변환할 때 같은 그림은 다시 호출하지 않는다.

실행
  python -m src.vlm_trial                      # 기본 5개 (종류별 1개), gemini
  python -m src.vlm_trial --vlm openai --n 3
  python -m src.vlm_trial --ids housing_pension_2026__p41__f2 ...
  python -m src.vlm_trial --env C:/Users/USER/Desktop/aim_home/.env   # 다른 위치의 .env 사용
출력: data/figures/vlm_trial.md (사람이 읽는 비교표 + 원본 이미지 링크)
"""
import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from src.components import vlm as vlm_reg
from src.figures import MD_PROMPT, number_check, vlm_md

ROOT = Path(__file__).resolve().parent.parent
FIG = ROOT / "data" / "figures"

# 종류별 대표 1개씩 (그 종류가 없으면 건너뜀). 순서 = 보여 줄 순서
PICKS = [
    ("글자가 도형으로 바뀐 차트", lambda f: f["route"] == "ocr_text" and f["file"].startswith("retirement")),
    ("화살표 흐름도", lambda f: f["kind"] == "diagram" and f["file"].startswith("housing_pension")),
    ("벡터 차트", lambda f: f["kind"] == "vector_chart" and f["route"] == "text_layer" and "그림" in (f["caption"] or "")),
    ("값이 빠진 표", lambda f: f["kind"] == "lossy_table" and (f["caption"] or "").startswith("[표")),
    ("비트맵 이미지", lambda f: f["kind"] == "raster" and (f["caption"] or "").startswith(("[그림", "그림", "<그림"))),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vlm", default="gemini")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--env", default=str(Path(__file__).resolve().parent.parent / ".env"),
                    help="API 키가 든 .env 경로 (기본: 프로젝트 폴더의 .env)")
    a = ap.parse_args()
    load_dotenv(a.env)

    figs = [json.loads(l) for l in open(FIG / "figures.jsonl", encoding="utf-8")]
    figs = [f for f in figs if not f["decorative"] and not f["dup_of"]]
    if a.ids:
        chosen = [("지정", f) for f in figs if f["figure_id"] in a.ids]
    else:
        chosen = []
        for label, cond in PICKS[:a.n]:
            f = next((f for f in figs if cond(f)), None)
            if f:
                chosen.append((label, f))

    describe = vlm_reg.REGISTRY.create(a.vlm)   # 키 없으면 여기서 바로 이유와 함께 중단
    lines = [f"# VLM 시험 변환 ({a.vlm})\n"]
    tot_in = tot_out = calls = 0
    for label, f in chosen:
        png = (ROOT / f["image_path"]).read_bytes()
        r = vlm_md(describe, a.vlm, png, f["caption"])
        ref = f["text_layer"] + "\n" + (f.get("ocr_text") or "") + "\n" + (f["caption"] or "")  # 캡션 숫자도 정당한 출처
        nc = number_check(r["md"], ref)
        if not r["cached"]:
            calls += 1
            tot_in += r["tokens_in"] or 0
            tot_out += r["tokens_out"] or 0
        print(f"[{label}] {f['figure_id']}  숫자검증 {nc}  토큰 {r['tokens_in']}/{r['tokens_out']}  "
              f"{r['seconds']}초{' (캐시)' if r['cached'] else ''}")
        print("   " + r["md"][:200].replace("\n", "\n   ") + "\n")
        lines += [
            f"## {label} — `{f['figure_id']}`",
            f"- 캡션: {f['caption']}",
            f"- 종류/경로: {f['kind']} / {f['route']}  ·  숫자 검증(원문에 있는 숫자 비율): **{nc}**",
            f"- 토큰 입력/출력: {r['tokens_in']}/{r['tokens_out']}  ·  {r['seconds']}초  ·  모델 {r['model']}",
            f"\n![{f['figure_id']}](img/{f['figure_id']}.png)\n",
            "<details><summary>원문 텍스트 레이어 / OCR (숫자 검증 기준)</summary>\n",
            f"```\n{ref.strip()[:800]}\n```\n</details>\n",
            "**Gemini Markdown**\n", r["md"], "\n---\n",
        ]
    lines.insert(1, f"호출 {calls}회 · 입력 토큰 {tot_in:,} · 출력 토큰 {tot_out:,}\n")
    out = FIG / "vlm_trial.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"호출 {calls}회 · 입력 토큰 {tot_in:,} · 출력 토큰 {tot_out:,}\n-> {out}")


if __name__ == "__main__":
    main()
