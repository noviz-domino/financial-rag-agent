"""Gemini 클라이언트 래퍼: 디스크 캐시 + 재시도 + 호출·토큰 집계.

- 모든 AI 호출은 CachedClient.call(task=...) 을 거친다. 같은 (모델, 프롬프트, 설정, 이미지)는 캐시에서 읽어 비용 0.
  캐시에서 읽어도 호출 수·토큰은 문항 비용(Meter)에 그대로 센다 (RAG vs Agent 비용 비교는 '쓴 만큼'이 기준).
- 백엔드: GeminiBackend(실제), FakeBackend(오프라인 --dry-run·테스트). 테스트는 네트워크를 절대 쓰지 않는다.
- 임베딩은 SQLite 캐시(키 = 모델+차원+용도+텍스트 해시)라서 내용이 바뀐 청크만 다시 임베딩한다.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from src.common.schemas import Usage

RETRYABLE = ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500", "INTERNAL", "timeout", "DEADLINE", "JSONDecode")


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    text: str
    data: object = None            # json_mode 면 파싱 결과
    tokens_in: int = 0
    tokens_out: int = 0
    cached: bool = False
    seconds: float = 0.0
    model: str = ""


class Meter:
    """문항 하나에서 쓴 AI 호출·검색·토큰·시간 합계. with client.meter() as m: ... 로 연다."""

    def __init__(self):
        self.usage = Usage()
        self._t0 = time.time()
        self._lock = threading.Lock()

    def add_llm(self, r: LLMResult):
        with self._lock:
            self.usage.llm_calls += 1
            self.usage.cached_calls += int(r.cached)
            self.usage.tokens_in += r.tokens_in
            self.usage.tokens_out += r.tokens_out

    def add_search(self, n=1):
        with self._lock:
            self.usage.search_calls += n

    @property
    def llm_calls(self):
        return self.usage.llm_calls

    def finish(self) -> dict:
        self.usage.elapsed = round(time.time() - self._t0, 3)
        return self.usage.to_dict()


_current_meter: contextvars.ContextVar[Meter | None] = contextvars.ContextVar("meter", default=None)


def current_meter() -> Meter | None:
    return _current_meter.get()


@contextlib.contextmanager
def use_meter(m: Meter | None = None):
    m = m or Meter()
    tok = _current_meter.set(m)
    try:
        yield m
    finally:
        _current_meter.reset(tok)


def parse_json_text(text: str):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, flags=re.S)
        if m:
            return json.loads(m.group(0))
        raise


# ------------------------------------------------------------------ 백엔드
class GeminiBackend:
    name = "gemini"
    cacheable = True

    def __init__(self, api_key: str | None = None):
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise LLMError("GEMINI_API_KEY 가 없어요. .env 에 넣거나 --dry-run 으로 실행하세요.")
        from google import genai
        self._genai = genai
        self._client = genai.Client(api_key=key)

    def generate(self, model, prompt, system, json_mode, images, temperature, max_tokens, ctx=None):
        from google.genai import types
        parts = [types.Part.from_bytes(data=im, mime_type="image/png") for im in (images or [])]
        parts.append(prompt)
        cfg = types.GenerateContentConfig(
            system_instruction=system, temperature=temperature, max_output_tokens=max_tokens,
            response_mime_type="application/json" if json_mode else None)
        t0 = time.time()
        resp = self._client.models.generate_content(model=model, contents=parts, config=cfg)
        um = resp.usage_metadata
        tin = getattr(um, "prompt_token_count", 0) or 0
        tout = (getattr(um, "candidates_token_count", 0) or 0) + (getattr(um, "thoughts_token_count", 0) or 0)
        return resp.text or "", tin, tout, time.time() - t0

    def embed(self, model, texts, dim, task_type):
        from google.genai import types
        out = []
        for i in range(0, len(texts), 100):
            r = self._client.models.embed_content(
                model=model, contents=texts[i:i + 100],
                config=types.EmbedContentConfig(task_type=task_type, output_dimensionality=dim))
            out += [e.values for e in r.embeddings]
        return out


class FakeBackend:
    """오프라인용 가짜 AI. task 이름별 핸들러(ctx -> dict|str)로 결정적 응답을 만든다. 캐시하지 않는다."""
    name = "fake"
    cacheable = False

    def __init__(self, handlers: dict | None = None):
        from src.common.fake_handlers import DEFAULT_HANDLERS
        self.handlers = {**DEFAULT_HANDLERS, **(handlers or {})}
        self.calls: list[tuple[str, dict]] = []

    def generate(self, model, prompt, system, json_mode, images, temperature, max_tokens, ctx=None):
        task = (ctx or {}).get("task", "")
        self.calls.append((task, ctx or {}))
        h = self.handlers.get(task)
        if h is None:
            raise LLMError(f"FakeBackend 에 task 핸들러가 없어요: {task!r}")
        out = h(ctx or {})
        text = json.dumps(out, ensure_ascii=False) if not isinstance(out, str) else out
        return text, max(1, len(prompt) // 3), max(1, len(text) // 3), 0.0

    def embed(self, model, texts, dim, task_type):
        return [fake_embedding(t, dim) for t in texts]


def fake_embedding(text: str, dim: int = 64) -> list[float]:
    """글자 2-gram 해시 벡터 (결정적, 오프라인). 겹치는 글자가 많을수록 코사인 유사도가 높다."""
    v = np.zeros(dim, dtype=np.float32)
    s = re.sub(r"\s+", "", text)
    for i in range(len(s) - 1):
        h = int(hashlib.md5(s[i:i + 2].encode("utf-8")).hexdigest()[:8], 16)
        v[h % dim] += 1.0
    n = float(np.linalg.norm(v))
    return (v / n if n else v).tolist()


# ------------------------------------------------------------------ 캐시 클라이언트
class CachedClient:
    def __init__(self, backend, cache_dir: Path, retries: int = 4, sleep=time.sleep):
        self.backend = backend
        self.cache_dir = Path(cache_dir)
        self.retries = retries
        self._sleep = sleep
        self._db = None
        self._db_lock = threading.Lock()

    # ---- 생성
    def _key(self, **kw) -> str:
        return hashlib.sha256(json.dumps(kw, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def call(self, task: str, model: str, prompt: str, *, system: str | None = None, json_mode: bool = True,
             images: list[bytes] | None = None, temperature: float = 0, max_tokens: int | None = 2048,
             ctx: dict | None = None) -> LLMResult:
        """ctx: FakeBackend 전용 구조화 입력 (실제 백엔드와 캐시 키에는 쓰이지 않음)."""
        ctx = {**(ctx or {}), "task": task}
        key = self._key(b=self.backend.name, m=model, p=prompt, s=system, j=json_mode, t=temperature, mt=max_tokens,
                        im=[hashlib.sha256(i).hexdigest() for i in (images or [])])
        path = self.cache_dir / "llm" / key[:2] / f"{key}.json"
        meter = current_meter()
        if self.backend.cacheable and path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            r = LLMResult(d["text"], d.get("data"), d["tokens_in"], d["tokens_out"], True, 0.0, model)
            if meter:
                meter.add_llm(r)
            return r
        last = None
        for attempt in range(self.retries + 1):
            try:
                text, tin, tout, sec = self.backend.generate(model, prompt, system, json_mode, images, temperature,
                                                             max_tokens, ctx)
                data = parse_json_text(text) if json_mode else None
                r = LLMResult(text, data, tin, tout, False, sec, model)
                break
            except Exception as e:  # noqa: BLE001 - SDK 마다 예외 타입이 달라 문자열로 판단
                last = e
                if attempt < self.retries and any(k in f"{type(e).__name__} {e}" for k in RETRYABLE):
                    self._sleep(min(60, 2 ** attempt * 2))
                    continue
                raise LLMError(f"{task} 호출 실패: {e}") from e
        else:  # pragma: no cover
            raise LLMError(str(last))
        if self.backend.cacheable:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"text": r.text, "data": r.data, "tokens_in": r.tokens_in,
                                        "tokens_out": r.tokens_out, "model": model, "task": task},
                                       ensure_ascii=False), encoding="utf-8")
        if meter:
            meter.add_llm(r)
        return r

    # ---- 임베딩 (SQLite 캐시)
    def _conn(self):
        if self._db is None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            name = "embed.sqlite" if self.backend.cacheable else "embed_fake.sqlite"
            self._db = sqlite3.connect(str(self.cache_dir / name), check_same_thread=False)
            self._db.execute("CREATE TABLE IF NOT EXISTS emb (k TEXT PRIMARY KEY, v BLOB)")
        return self._db

    def embed(self, texts: list[str], model: str, dim: int, task_type: str = "RETRIEVAL_DOCUMENT"):
        """반환: (N, dim) float32 정규화 배열, 새로 임베딩한 개수."""
        keys = [hashlib.sha256(f"{self.backend.name}|{model}|{dim}|{task_type}|{t}".encode("utf-8")).hexdigest()
                for t in texts]
        with self._db_lock:
            db = self._conn()
            found = {}
            for i in range(0, len(keys), 500):
                part = keys[i:i + 500]
                q = ",".join("?" * len(part))
                for k, v in db.execute(f"SELECT k, v FROM emb WHERE k IN ({q})", part):
                    found[k] = np.frombuffer(v, dtype=np.float32)
        todo = [i for i, k in enumerate(keys) if k not in found]
        # 같은 텍스트 중복은 한 번만
        uniq = {}
        for i in todo:
            uniq.setdefault(keys[i], texts[i])
        new = 0
        items = list(uniq.items())
        for s in range(0, len(items), 100):
            batch = items[s:s + 100]
            for attempt in range(self.retries + 1):
                try:
                    vecs = self.backend.embed(model, [t for _, t in batch], dim, task_type)
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt < self.retries and any(k in f"{type(e).__name__} {e}" for k in RETRYABLE):
                        self._sleep(min(60, 2 ** attempt * 5))
                        continue
                    raise LLMError(f"임베딩 실패: {e}") from e
            with self._db_lock:
                db = self._conn()
                for (k, _), v in zip(batch, vecs):
                    a = np.asarray(v, dtype=np.float32)
                    n = float(np.linalg.norm(a))
                    a = a / n if n else a
                    db.execute("INSERT OR REPLACE INTO emb VALUES (?, ?)", (k, a.tobytes()))
                    found[k] = a
                db.commit()
            new += len(batch)
        arr = np.stack([found[k] for k in keys]) if keys else np.zeros((0, dim), dtype=np.float32)
        return arr, new

    def meter(self):
        return use_meter()


def make_client(cfg: dict, *, offline: bool, handlers: dict | None = None, sleep=time.sleep) -> CachedClient:
    """offline=True 면 FakeBackend (API 키 불필요). 아니면 .env 의 GEMINI_API_KEY 로 Gemini."""
    from src.common.config import data_dir
    cache = data_dir(cfg) / "cache"
    if offline:
        return CachedClient(FakeBackend(handlers), cache / "fake", sleep=sleep)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:  # pragma: no cover
        pass
    return CachedClient(GeminiBackend(), cache, sleep=sleep)
