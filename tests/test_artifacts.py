"""임베딩·캐시 산출물 묶기/풀기 (작은 가짜 data 로)."""
import json
import sqlite3
import zipfile

import pytest

from src.common.config import load_config
from src.pipeline import artifacts as A


def _make_data(d):
    (d / "chunks").mkdir(parents=True)
    (d / "chunks" / "structural.jsonl").write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
    (d / "chunks" / "structural.parents.jsonl").write_text("", encoding="utf-8")
    for f in A.INCLUDE_FILES:
        (d / f).write_text("{}\n", encoding="utf-8")
    (d / "cache" / "llm" / "ab").mkdir(parents=True)
    (d / "cache" / "llm" / "ab" / "x.json").write_text("{}", encoding="utf-8")
    (d / "cache" / "fake").mkdir()
    (d / "cache" / "fake" / "y.json").write_text("{}", encoding="utf-8")
    sqlite3.connect(d / "cache" / "embed.sqlite").close()
    (d / "cache" / "embed_fake.sqlite").write_bytes(b"fake")
    ok = d / "index" / "structural__text__gemini-embedding-001__768"
    ok.mkdir(parents=True)
    (ok / "chroma.sqlite3").write_bytes(b"c" * 100)
    fk = d / "index" / "structural__text__gemini-embedding-001__768__fake"
    fk.mkdir()
    (fk / "chroma.sqlite3").write_bytes(b"f")
    (d / "figures" / "img").mkdir(parents=True)
    (d / "figures" / "img" / "a.png").write_bytes(b"png")
    (d / "figures" / "corrections.jsonl").write_text("", encoding="utf-8")


@pytest.fixture
def env(tmp_path):
    d = tmp_path / "data"
    _make_data(d)
    pdfs = tmp_path / "pdf"
    pdfs.mkdir()
    (pdfs / "a.pdf").write_bytes(b"%PDF-1")
    return d, pdfs, tmp_path / "dist", load_config("configs/baseline.json")


def test_collect_skips_fake_and_images(env):
    d, *_ = env
    files = A.collect_files(d)
    assert not any("fake" in f for f in files) and not any("img/" in f for f in files)
    assert "cache/embed.sqlite" in files and "cache/llm/ab/x.json" in files
    assert any(f.startswith("index/structural__text__gemini-embedding-001__768/") for f in files)
    assert any("img/" in f for f in A.collect_files(d, with_images=True))


def test_export_import_roundtrip_and_manifest(env, tmp_path):
    d, pdfs, out, cfg = env
    paths = A.export(d, out, pdfs, cfg, tag="t", log=lambda *_: None)
    man = json.loads(zipfile.ZipFile(paths[0]).read("manifest.json"))
    assert man["embed_models"] == ["gemini-embedding-001"] and man["embed_dims"] == [768]
    assert man["chunk_variants"]["structural"]["n_chunks"] == 2 and man["pdf"]["n_pdfs"] == 1
    d2 = tmp_path / "data2"
    A.import_(paths, d2, pdfs, cfg, log=lambda *_: None)
    assert (d2 / "cache" / "embed.sqlite").exists() and (d2 / "chunks" / "structural.jsonl").exists()
    assert (d2 / "index" / "structural__text__gemini-embedding-001__768" / "chroma.sqlite3").read_bytes() == b"c" * 100


def test_split_into_parts_under_limit(env):
    d, pdfs, out, cfg = env
    (d / "cache" / "llm" / "ab" / "big.json").write_bytes(b"x" * 3000)
    paths = A.export(d, out, pdfs, cfg, max_bytes=2000, log=lambda *_: None)
    assert len(paths) > 1
    names = {n for p in paths for n in zipfile.ZipFile(p).namelist() if n.startswith("data/")}
    assert "data/cache/llm/ab/big.json" in names and "data/cache/embed.sqlite" in names      # 빠진 파일 없음
    with pytest.raises(SystemExit):                                                          # part 하나만 주면 거부
        A.import_(paths[:1], d.parent / "d3", pdfs, cfg, log=lambda *_: None)
    A.import_(paths, d.parent / "d3", pdfs, cfg, log=lambda *_: None)


def test_stale_detected_when_pdf_or_config_differs(env, tmp_path):
    d, pdfs, out, cfg = env
    paths = A.export(d, out, pdfs, cfg, log=lambda *_: None)
    (pdfs / "b.pdf").write_bytes(b"%PDF-2")                          # 원본 PDF 가 바뀜
    with pytest.raises(SystemExit):
        A.import_(paths, tmp_path / "x", pdfs, cfg, log=lambda *_: None)
    A.import_(paths, tmp_path / "x", pdfs, cfg, force=True, log=lambda *_: None)
    other = load_config("configs/exp6_embed2.json")                  # 다른 임베딩 모델 → 묶음에 색인 없음
    (pdfs / "b.pdf").unlink()
    probs = A.check_manifest(A._read_manifest(paths[0]), paths, other, pdfs)
    assert any("색인" in p for p in probs)
    assert A.check_manifest(A._read_manifest(paths[0]), paths, cfg, pdfs) == []


def test_corrupted_file_detected(env, tmp_path):
    d, pdfs, out, cfg = env
    (p,) = A.export(d, out, pdfs, cfg, log=lambda *_: None)
    man = json.loads(zipfile.ZipFile(p).read("manifest.json"))
    man["files"]["chunks/structural.jsonl"]["sha256"] = "0" * 64
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(p) as zin, zipfile.ZipFile(bad, "w") as zout:
        for n in zin.namelist():
            zout.writestr(n, json.dumps(man) if n == "manifest.json" else zin.read(n))
    with pytest.raises(SystemExit):
        A.import_([bad], tmp_path / "y", pdfs, cfg, force=True, log=lambda *_: None)
