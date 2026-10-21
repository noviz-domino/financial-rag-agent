"""임베딩·캐시·색인 산출물을 팀이 같이 쓰도록 묶고 푸는 도구 (한 번 쓴 API 비용을 다시 내지 않기 위해).

  python -m src.pipeline.artifacts export [--out dist] [--max-gb 1.9] [--tag 이름] [--with-images]
  python -m src.pipeline.artifacts import dist/artifacts_xxx_part01.zip [dist/..._part02.zip ...] [--config configs/baseline.json] [--force]

export: data/ 의 Gemini 응답·임베딩 캐시(cache/), Chroma 색인(index/, 가짜 AI 것은 제외), 청크·그림 설명·쪽/근거 단위를
        버전이 붙은 zip(들)로 dist/ 에 만든다 (git 에 올리지 않음). 한 파일이 max-gb 를 넘으면 여러 개로 나눈다.
        모든 zip 에 manifest.json (만든 날짜, 모델·차원, 청킹 변형별 설정 해시·청크 수, 색인 목록, PDF 지문, 파일별 sha256)
import: zip 을 data/ 에 풀고 manifest 를 검사한다 — 파일 손상(sha256), 부분 누락, PDF 가 바뀐 경우(stale),
        현재 설정의 청킹·임베딩 모델이 묶음에 없는 경우를 알려 준다. 문제가 있으면 --force 가 아니면 풀지 않는다.
GitHub Release 로 올리는 방법은 README 1장 참고 (이 도구는 아무것도 업로드하지 않는다).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

from src.common.config import ROOT, chunk_variant_name, data_dir, load_config

FORMAT_VERSION = 1
GB = 1024 ** 3
# data/ 아래에서 묶을 것 (가짜 AI 캐시·임시 파일은 제외)
INCLUDE_FILES = ["pages.jsonl", "pages_meta.json", "sources.jsonl", "figures.jsonl", "figure_review.jsonl"]
INCLUDE_DIRS = ["cache", "index", "chunks", "figures"]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _skip(rel: str, with_images: bool) -> bool:
    parts = rel.split("/")
    if "__fake" in rel or parts[-1] == "embed_fake.sqlite" or (parts[0] == "cache" and "fake" in parts[1:2]):
        return True
    if not with_images and rel.startswith("figures/img/"):
        return True
    return rel.endswith((".lock", "-journal", "-wal", "-shm"))


def collect_files(d: Path, with_images=False) -> list[str]:
    out = [f for f in INCLUDE_FILES if (d / f).is_file()]
    for sub in INCLUDE_DIRS:
        base = d / sub
        if base.is_dir():
            out += [p.relative_to(d).as_posix() for p in sorted(base.rglob("*")) if p.is_file()]
    return [r for r in out if not _skip(r, with_images)]


def pdf_fingerprint(pdf_root: Path) -> dict:
    """원본 PDF 목록 + 크기 → 해시. PDF 가 바뀌면 청크·임베딩이 낡은 것이다."""
    items = sorted((p.relative_to(pdf_root).as_posix(), p.stat().st_size) for p in pdf_root.rglob("*.pdf")) \
        if pdf_root.exists() else []
    return {"n_pdfs": len(items), "hash": hashlib.sha256(json.dumps(items).encode()).hexdigest()[:16]}


def _chunk_infos(d: Path) -> dict:
    out = {}
    for p in sorted((d / "chunks").glob("*.jsonl")) if (d / "chunks").is_dir() else []:
        if p.name.endswith(".parents.jsonl"):
            continue
        with open(p, encoding="utf-8") as f:
            n = sum(1 for l in f if l.strip())
        out[p.stem] = {"n_chunks": n, "sha256": _sha256(p)[:16]}
    return out


def _index_infos(d: Path, files: list[str]) -> list[dict]:
    names = sorted({r.split("/")[1] for r in files if r.startswith("index/") and r.count("/") >= 2})
    out = []
    for n in names:
        m = re.match(r"(?P<variant>.+?)__(?P<field>text|embed_text)__(?P<model>.+)__(?P<dim>\d+)$", n)
        out.append({"dir": n, **(m.groupdict() if m else {})})
    return out


def build_manifest(d: Path, files: list[str], pdf_root: Path, cfg: dict | None, tag: str = "") -> dict:
    sizes = {r: (d / r).stat().st_size for r in files}
    idx = _index_infos(d, files)
    models = sorted({i["model"] for i in idx if "model" in i})
    return {
        "format": FORMAT_VERSION, "created": dt.datetime.now().isoformat(timespec="seconds"), "tag": tag,
        "git_commit": _git_commit(), "pdf": pdf_fingerprint(pdf_root),
        "embed_models": models, "embed_dims": sorted({int(i["dim"]) for i in idx if "dim" in i}),
        "indexes": idx, "chunk_variants": _chunk_infos(d),
        "config_hash": _config_hash(cfg) if cfg else None,
        "counts": {"files": len(files), "bytes": sum(sizes.values()),
                   "llm_cache_files": sum(1 for r in files if r.startswith("cache/llm/")),
                   "embed_cache": [r for r in files if r.startswith("cache/") and r.endswith(".sqlite")]},
        "files": {r: {"size": sizes[r], "sha256": _sha256(d / r)} for r in files},
    }


def _config_hash(cfg: dict) -> str:
    keep = {"figures": {k: cfg["figures"].get(k) for k in ("model", "dpi", "ocr_engine", "max_output_chars")},
            "tables": cfg["tables"], "loading": cfg["loading"]}
    return hashlib.sha256(json.dumps(keep, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _git_commit():
    import subprocess
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001
        return None


def split_parts(sizes: dict[str, int], max_bytes: int) -> list[list[str]]:
    """파일을 순서대로 담되 한 묶음이 max_bytes 를 넘지 않게 나눈다 (파일 하나가 더 크면 혼자 한 묶음)."""
    parts, cur, cur_size = [], [], 0
    for r, s in sizes.items():
        if cur and cur_size + s > max_bytes:
            parts.append(cur)
            cur, cur_size = [], 0
        cur.append(r)
        cur_size += s
    if cur:
        parts.append(cur)
    return parts or [[]]


def export(d: Path, out_dir: Path, pdf_root: Path, cfg: dict | None = None, *, max_bytes=int(1.9 * GB), tag="",
           with_images=False, log=print) -> list[Path]:
    files = collect_files(d, with_images)
    if not files:
        raise SystemExit(f"{d} 에 묶을 파일이 없어요. 먼저 build 를 실행하세요.")
    man = build_manifest(d, files, pdf_root, cfg, tag)
    parts = split_parts({r: man["files"][r]["size"] for r in files}, max_bytes)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    base = f"artifacts_{stamp}" + (f"_{re.sub(r'[^0-9A-Za-z._-]', '_', tag)}" if tag else "")
    out_dir.mkdir(parents=True, exist_ok=True)
    man["parts"] = [f"{base}_part{i + 1:02d}.zip" for i in range(len(parts))]
    paths = []
    for name, members in zip(man["parts"], parts):
        p = out_dir / name
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(man, ensure_ascii=False, indent=2))
            for r in members:
                z.write(d / r, "data/" + r)
        log(f"  {p}  ({p.stat().st_size / 1024 ** 2:.1f} MB, 파일 {len(members)}개)")
        paths.append(p)
    return paths


def _read_manifest(zpath: Path) -> dict:
    with zipfile.ZipFile(zpath) as z:
        return json.loads(z.read("manifest.json"))


def check_manifest(man: dict, zips: list[Path], cfg: dict | None, pdf_root: Path) -> list[str]:
    """문제 목록 (비어 있으면 통과)."""
    problems = []
    if man.get("format") != FORMAT_VERSION:
        problems.append(f"묶음 형식 {man.get('format')} 은 이 코드({FORMAT_VERSION})와 달라요")
    have = {p.name for p in zips}
    missing = [n for n in man.get("parts", []) if n not in have]
    if missing:
        problems.append(f"일부 파일이 없어요: {missing} (모든 part zip 을 같이 주세요)")
    now = pdf_fingerprint(pdf_root)
    if man["pdf"]["hash"] != now["hash"]:
        problems.append(f"원본 PDF 가 달라요 (묶음 {man['pdf']}, 현재 {now}) → 청크·임베딩이 낡았을 수 있어요")
    if cfg:
        r = cfg["retrieval"]
        variant = chunk_variant_name(cfg)
        if variant not in man["chunk_variants"]:
            problems.append(f"현재 설정의 청킹 {variant} 가 묶음에 없어요 (있는 것: {sorted(man['chunk_variants'])})")
        want = {"variant": variant, "field": r["embed_field"], "model": re.sub(r"[^A-Za-z0-9._-]", "_", r["embed_model"]),
                "dim": str(r["embed_dim"])}
        if not any(all(i.get(k) == v for k, v in want.items()) for i in man["indexes"]):
            problems.append(f"현재 설정의 색인 {want} 가 묶음에 없어요")
        if man.get("config_hash") and man["config_hash"] != _config_hash(cfg):
            problems.append("그림·표·읽기 설정이 묶음을 만들 때와 달라요 (그림 설명 캐시를 못 쓸 수 있어요)")
    return problems


def import_(zips: list[Path], d: Path, pdf_root: Path, cfg: dict | None = None, *, force=False, log=print) -> dict:
    zips = [Path(z) for z in zips]
    man = _read_manifest(zips[0])
    problems = check_manifest(man, zips, cfg, pdf_root)
    for p in problems:
        log("  [검사] " + p)
    if problems and not force:
        raise SystemExit("묶음이 현재 상태와 맞지 않아요. 그래도 풀려면 --force 를 붙이세요.")
    n = 0
    for zp in zips:
        with zipfile.ZipFile(zp) as z:
            for name in z.namelist():
                if not name.startswith("data/") or name.endswith("/"):
                    continue
                rel = name[len("data/"):]
                if ".." in Path(rel).parts:
                    raise SystemExit(f"위험한 경로: {name}")
                dest = d / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(name) as src, open(dest, "wb") as out:
                    while chunk := src.read(1 << 20):
                        out.write(chunk)
                n += 1
    in_zips = set()
    for zp in zips:
        with zipfile.ZipFile(zp) as z:
            in_zips |= {x[len("data/"):] for x in z.namelist() if x.startswith("data/")}
    bad = [r for r in in_zips if r in man["files"] and _sha256(d / r) != man["files"][r]["sha256"]]
    if bad:
        raise SystemExit(f"풀린 파일이 손상됐어요 (sha256 불일치): {bad[:5]}")
    log(f"  {n}개 파일을 {d} 에 풀었어요 (sha256 확인 완료, 만든 날 {man['created']}, 모델 {man['embed_models']})")
    return man


def release_command(paths: list[Path], tag: str) -> str:
    files = " ".join(f'"{p.as_posix()}"' for p in paths)
    return (f'gh release create {("data-" + tag) if tag else "data-v1"} {files} --title "임베딩·캐시 산출물 {tag or "data-v1"}" '
            f'--notes "python -m src.pipeline.artifacts import 로 data/ 에 풀어요 (README 1장 참고)"')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--config", default="configs/baseline.json")
    e.add_argument("--out", default="dist")
    e.add_argument("--max-gb", type=float, default=1.9, help="zip 하나의 최대 크기 (GitHub Release 한 파일 2GB 한도)")
    e.add_argument("--tag", default="", help="이름 뒤에 붙일 표시 (예: baseline)")
    e.add_argument("--with-images", action="store_true", help="그림 PNG(data/figures/img)도 포함 (기본은 설명만)")
    i = sub.add_parser("import")
    i.add_argument("zips", nargs="+")
    i.add_argument("--config", default="configs/baseline.json")
    i.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    d, pdf_root = data_dir(cfg), ROOT / "01_data"
    if a.cmd == "export":
        out = Path(a.out)
        out = out if out.is_absolute() else ROOT / out
        paths = export(d, out, pdf_root, cfg, max_bytes=int(a.max_gb * GB), tag=a.tag, with_images=a.with_images)
        print("\nGitHub Release 로 올리려면 (직접 실행하세요, 이 도구는 업로드하지 않아요):\n  "
              + release_command(paths, a.tag))
    else:
        import_(a.zips, d, pdf_root, cfg, force=a.force)


if __name__ == "__main__":
    sys.exit(main())
