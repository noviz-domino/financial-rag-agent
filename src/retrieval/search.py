"""검색 도구 (기획서 3-5). search(query, k, scope) / get_parent(chunk_id) / follow_ref(chunk_id).

  - scope(doc_type 목록 또는 'base' 같은 이름)는 **필수**. 빠뜨리면 ScopeRequiredError.
  - 모드: vector / bm25 / hybrid(RRF, k=60), 설정 retrieval.mode.
  - retrieval.parent_fold=true 이면 child 를 parent(조항 전체·반쪽 페이지)로 접어 돌려준다 (청킹 실험 1).
반환: [{chunk_id, parent_id, text, file, pages, score, content_type, doc_type, section, source_ids, half}]
"""
from __future__ import annotations

from src.common.config import chunk_variant_name, data_dir
from src.common.llm import current_meter
from src.common.schemas import DOC_TYPES

SCOPE_NAMES = {"base": ["glossary", "terms"], "life_guide": ["life_guide"], "additional": ["additional"],
               "all": list(DOC_TYPES)}


class ScopeRequiredError(ValueError):
    pass


def normalize_scope(scope) -> list[str]:
    if scope is None or (not isinstance(scope, str) and len(scope) == 0) or scope == "":
        raise ScopeRequiredError("search() 는 scope(검색할 doc_type 목록)를 꼭 받아야 해요")
    if isinstance(scope, str):
        scope = SCOPE_NAMES.get(scope) or [scope]
    bad = [s for s in scope if s not in DOC_TYPES]
    if bad:
        raise ScopeRequiredError(f"알 수 없는 scope: {bad} (가능: {list(DOC_TYPES)})")
    return list(dict.fromkeys(scope))


def rrf_fuse(rank_lists: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion. 각 목록은 id 의 순위 순서. 점수 = Σ 1/(k+순위). 동점은 먼저 나온 id 가 앞."""
    score, first = {}, {}
    for lst in rank_lists:
        for r, i in enumerate(lst, start=1):
            score[i] = score.get(i, 0.0) + 1.0 / (k + r)
            first.setdefault(i, len(first))
    return sorted(score.items(), key=lambda kv: (-kv[1], first[kv[0]]))


def public(c: dict, score, rank=None, text=None) -> dict:
    return {"chunk_id": c["chunk_id"], "parent_id": c.get("parent_id"), "text": text if text is not None else c["text"],
            "file": c["file"], "pages": c["pages"], "score": score, "content_type": c["content_type"],
            "doc_type": c["doc_type"], "section": c.get("section", ""), "source_ids": c["source_ids"],
            "half": c.get("half")}


class Retriever:
    def __init__(self, cfg: dict, chunks: list[dict], parents: list[dict], client=None, vector=None, token_cache=None):
        from src.index.bm25 import TokenCache
        self.cfg, self.chunks, self.client, self.vector = cfg, chunks, client, vector
        self.by_id = {c["chunk_id"]: c for c in chunks}
        self.parents = {p["chunk_id"]: p for p in parents}
        self._bm25 = {}
        self._tokens = token_cache or TokenCache(None)

    @classmethod
    def from_config(cls, cfg: dict, client=None, build_if_missing=True, log=print):
        from src.index.bm25 import TokenCache
        from src.pipeline.build import ensure_chunks, read_jsonl
        d = data_dir(cfg)
        variant = ensure_chunks(cfg, log)
        chunks = read_jsonl(d / "chunks" / f"{variant}.jsonl")
        pp = d / "chunks" / f"{variant}.parents.jsonl"
        parents = read_jsonl(pp) if pp.exists() else []
        vector = None
        mode = cfg["retrieval"]["mode"]
        if mode in ("vector", "hybrid"):
            if client is None:
                raise ValueError(f"검색 모드 {mode} 는 임베딩 클라이언트가 필요해요 (--dry-run 이면 가짜 임베딩)")
            from src.index.store import VectorIndex, build_index, index_dir
            if not index_dir(cfg, client).exists() and build_if_missing:
                log("색인이 없어 만들어요 (임베딩 호출)")
                build_index(cfg, chunks, client, log)
            vector = VectorIndex(cfg, client)
        return cls(cfg, chunks, parents, client, vector, TokenCache(d / "cache" / "kiwi_tokens.json"))

    # ---- BM25 (범위별로 색인: IDF 가 범위 안 문서 기준)
    def _bm25_for(self, scope):
        key = tuple(sorted(scope))
        if key not in self._bm25:
            from src.index.bm25 import BM25Index
            self._bm25[key] = BM25Index([c for c in self.chunks if c["doc_type"] in scope], self._tokens)
            self._tokens.save()
        return self._bm25[key]

    def _rank(self, query: str, scope: list[str], mode: str, n: int):
        """[(chunk_id, score)] — mode 별 상위 n."""
        bm = [(c["chunk_id"], s) for c, s in self._bm25_for(scope).query(query, n)] if mode in ("bm25", "hybrid") else []
        vec = self.vector.query(query, n, scope) if mode in ("vector", "hybrid") else []
        if mode == "bm25":
            return bm
        if mode == "vector":
            return vec
        rrf_k = self.cfg["retrieval"].get("rrf_k", 60)
        return rrf_fuse([[i for i, _ in vec], [i for i, _ in bm]], rrf_k)[:n]

    def search(self, query: str, k: int | None = None, *, scope=None, mode: str | None = None) -> list[dict]:
        scope = normalize_scope(scope)
        r = self.cfg["retrieval"]
        k = k or r["k"]
        mode = mode or r["mode"]
        m = current_meter()
        if m:
            m.add_search()
        fetch = max(r.get("fetch_k", 20), k)
        ranked = self._rank(query, scope, mode, fetch)
        if r.get("parent_fold") and self.parents:
            return self._fold(ranked, k)
        return [public(self.by_id[i], s) for i, s in ranked[:k]]

    def _fold(self, ranked, k):
        out, seen = [], set()
        for i, s in ranked:
            c = self.by_id[i]
            pid = c.get("parent_id")
            p = self.parents.get(pid)
            if p is None:
                out.append(public(c, s))
                continue
            if pid in seen:
                continue
            seen.add(pid)
            kids = [self.by_id[x] for x in p["child_ids"] if x in self.by_id]
            row = public(c, s, text=p["text"])
            row.update(chunk_id=pid, child_id=i, pages=p["pages"], section=p.get("section", ""),
                       source_ids=sorted({x for ch in kids for x in ch["source_ids"]}), content_type="text")
            out.append(row)
            if len(out) >= k:
                break
        return out[:k]

    def get_parent(self, chunk_id: str) -> dict | None:
        """조항 전체·반쪽 페이지. parent 가 없으면(recursive 등) None."""
        c = self.by_id.get(chunk_id)
        pid = c.get("parent_id") if c else chunk_id
        p = self.parents.get(pid)
        if not p:
            return None
        return {"chunk_id": p["chunk_id"], "parent_id": None, "text": p["text"], "file": p["file"], "pages": p["pages"],
                "score": None, "content_type": "text", "doc_type": p["doc_type"], "section": p.get("section", ""),
                "source_ids": sorted({x for cid in p["child_ids"] for x in self.by_id.get(cid, {}).get("source_ids", [])}),
                "half": p.get("half")}

    def follow_ref(self, chunk_id: str) -> list[dict]:
        """링크로 연결된 조항·그림 청크 (article_refs + figure_refs)."""
        c = self.by_id.get(chunk_id)
        if not c:
            return []
        out = []
        for ref in c["links"].get("article_refs", []) + c["links"].get("figure_refs", []):
            t = self.by_id.get(ref["target"])
            if t:
                out.append({**public(t, None), "ref_label": ref["label"]})
        return out


def retrieval_signature(cfg: dict) -> str:
    r = cfg["retrieval"]
    return f"{chunk_variant_name(cfg)}|{r['mode']}|k={r['k']}|fold={r.get('parent_fold')}|{r['embed_field']}"
