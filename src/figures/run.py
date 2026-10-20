"""그림 단계 조립: 후보 → 거르기 → OCR → (VLM 켜졌을 때) Gemini 설명 → 검사 → figures.jsonl / figure_review.jsonl.

  - --no-vlm 이면 Gemini 를 부르지 않는다 (그림 청크는 텍스트 레이어·OCR 글로 대체).
  - data/figures/corrections.jsonl 이 있으면 사람이 고친 설명을 덮어쓰고 correction_id 를 기록한다.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.figures.describe import FULL_KINDS, apply_checks, describe_figure
from src.figures.detect import extract_figures


def load_corrections(path: Path) -> dict:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            out[d["figure_id"]] = d
    return out


def build_figures(pdf_path: Path, page_records: list[dict], cfg: dict, ocr, client, out_dir: Path, use_vlm: bool):
    fc = cfg["figures"]
    figs = extract_figures(pdf_path, page_records, ocr, cfg)
    img_dir = out_dir / "img"
    img_dir.mkdir(parents=True, exist_ok=True)
    corrections = load_corrections(out_dir / "corrections.jsonl")
    by_id = {f["figure_id"]: f for f in figs}
    for f in figs:
        (img_dir / f"{f['figure_id']}.png").write_bytes(f["_png"])
        f["image_path"] = f"figures/img/{f['figure_id']}.png"
        f.update(description="", gemini_kind=None, md_source=None, num_check=None, unverified_numbers=[],
                 length_flag=None, caution=False, review_reasons=[], correction_id=None)
        if f["dup_of"] or f["decorative"] or not use_vlm or client is None:
            continue
        try:
            r = describe_figure(client, fc["model"], f, fc.get("max_output_chars", 4000))
        except Exception as e:  # noqa: BLE001 - 한 그림 실패가 전체 빌드를 멈추지 않게
            f["vlm_error"] = str(e)[:200]
            continue
        f.update(gemini_kind=r["gemini_kind"], description=r["description"], md_source=f"gemini:{fc['model']}")
        if f["gemini_kind"] == "decorative":
            f["decorative"], f["skip_reason"] = True, "gemini_decorative"
    for f in figs:
        c = corrections.get(f["figure_id"])
        if c:
            f.update(description=c["description"], correction_id=c.get("correction_id"), md_source="human_correction")
        if f["dup_of"]:   # 중복 그림은 원본의 결과를 공유
            src = by_id[f["dup_of"]]
            for k in ("description", "gemini_kind", "md_source", "ocr_text"):
                f[k] = src.get(k)
        if f.get("description"):
            apply_checks(f, fc.get("max_output_chars", 4000))
    return figs


def figure_style(f: dict) -> str:
    return "full" if f.get("gemini_kind") in FULL_KINDS else "short"
