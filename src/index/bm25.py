"""Kiwi 형태소 BM25. 토큰은 내용어(명사·동사·형용사·어근·영문·숫자)만, 디스크 캐시(텍스트 해시)로 재색인을 빠르게."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from rank_bm25 import BM25Okapi

_KEEP = ("NN", "NR", "VV", "VA", "XR", "SL", "SN", "SH", "MAG")
_KIWI = None


def _kiwi():
    global _KIWI
    if _KIWI is None:
        from kiwipiepy import Kiwi
        _KIWI = Kiwi()
    return _KIWI


def tokenize(text: str) -> list[str]:
    toks = []
    for t in _kiwi().tokenize(text or ""):
        if t.tag.startswith(_KEEP) and t.tag != "NNB":
            form = t.form.lower()
            if t.tag.startswith("V"):
                form += "다"
            toks.append(form)
    return toks


class TokenCache:
    """텍스트 해시 → 토큰 목록. data/cache/kiwi_tokens.json 한 파일."""

    def __init__(self, path: Path | None):
        self.path, self.d, self.dirty = path, {}, False
        if path and path.exists():
            self.d = json.loads(path.read_text(encoding="utf-8"))

    def get(self, text: str) -> list[str]:
        k = hashlib.sha1(text.encode("utf-8")).hexdigest()
        if k not in self.d:
            self.d[k] = tokenize(text)
            self.dirty = True
        return self.d[k]

    def save(self):
        if self.path and self.dirty:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.d, ensure_ascii=False), encoding="utf-8")
            self.dirty = False


class BM25Index:
    def __init__(self, chunks: list[dict], cache: TokenCache):
        self.chunks = chunks
        docs = [cache.get(c["text"]) for c in chunks]
        self.sets = [set(d) for d in docs]
        self.bm25 = BM25Okapi([d or ["_"] for d in docs])
        self.cache = cache

    def query(self, q: str, n: int):
        """[(chunk, score)] 점수 내림차순. 질문 토큰이 하나도 없는 청크는 제외 (작은 말뭉치에서 IDF 가 0 이하여도 순위가 유지됨)."""
        toks = tokenize(q)
        if not toks:
            return []
        scores = self.bm25.get_scores(toks)
        q = set(toks)
        order = [i for i in sorted(range(len(scores)), key=lambda i: -scores[i]) if q & self.sets[i]][:n]
        return [(self.chunks[i], float(scores[i])) for i in order]
