"""임베딩(캐시) → Chroma 저장. 내용이 바뀐 청크만 다시 임베딩 (client.embed 의 SQLite 캐시).

색인 위치: data/index/{청킹}__{임베딩필드}__{모델}__{차원}[__fake]/  (청킹·임베딩 설정이 다르면 다른 색인)
Chroma 는 requirements.txt 에서 버전 고정. 검색 범위는 where={"doc_type": {"$in": scope}} 로 강제한다.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import numpy as np

from src.common.config import chunk_variant_name, data_dir

COLLECTION = "chunks"


def index_dir(cfg: dict, client) -> Path:
    r = cfg["retrieval"]
    model = re.sub(r"[^A-Za-z0-9._-]", "_", r["embed_model"])
    name = f"{chunk_variant_name(cfg)}__{r['embed_field']}__{model}__{r['embed_dim']}"
    if not client.backend.cacheable:
        name += "__fake"
    return data_dir(cfg) / "index" / name


def _chroma(path: Path):
    import chromadb
    path.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(path))


def build_index(cfg: dict, chunks: list[dict], client, log=print, batch=256):
    """청크 임베딩 → Chroma. 이미 같은 내용이 들어 있으면 건너뛴다."""
    r = cfg["retrieval"]
    path = index_dir(cfg, client)
    db = _chroma(path)
    col = db.get_or_create_collection(COLLECTION, metadata={"hnsw:space": "cosine"})
    have = {}
    got = col.get(include=["metadatas"])
    for i, m in zip(got["ids"], got["metadatas"]):
        have[i] = (m or {}).get("h")
    field = r["embed_field"]
    todo = [c for c in chunks if have.get(c["chunk_id"]) != hashlib.sha1(c[field].encode("utf-8")).hexdigest()]
    stale = set(have) - {c["chunk_id"] for c in chunks}
    if stale:
        col.delete(ids=list(stale))
    new_total = 0
    for s in range(0, len(todo), batch):
        part = todo[s:s + batch]
        vecs, new = client.embed([c[field] for c in part], r["embed_model"], r["embed_dim"], "RETRIEVAL_DOCUMENT")
        new_total += new
        col.upsert(ids=[c["chunk_id"] for c in part], embeddings=vecs.tolist(),
                   metadatas=[{"doc_type": c["doc_type"], "file": c["file"], "content_type": c["content_type"],
                               "h": hashlib.sha1(c[field].encode("utf-8")).hexdigest()} for c in part])
    log(f"  색인 {path.name}: 청크 {len(chunks)}개 (새로 넣은 {len(todo)}개, 새로 임베딩 호출한 {new_total}개)")
    return path


class VectorIndex:
    def __init__(self, cfg: dict, client):
        self.cfg, self.client = cfg, client
        self.col = _chroma(index_dir(cfg, client)).get_collection(COLLECTION)

    def query(self, text: str, n: int, scope: list[str]):
        r = self.cfg["retrieval"]
        vec, _ = self.client.embed([text], r["embed_model"], r["embed_dim"], "RETRIEVAL_QUERY")
        res = self.col.query(query_embeddings=vec.tolist(), n_results=n, where={"doc_type": {"$in": list(scope)}})
        return [(i, 1.0 - d) for i, d in zip(res["ids"][0], res["distances"][0])]
