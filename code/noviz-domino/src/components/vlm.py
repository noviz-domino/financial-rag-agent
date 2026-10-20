"""멀티모달(VLM) 어댑터. 모든 구현: describe(png bytes, prompt) → VLMResult (토큰·시간 기록 포함).

토큰을 기록하는 이유: 과제가 기본 RAG vs Agent 비교에서 입력·출력 토큰과 호출 수 집계를 요구함.
"""
import base64
import os
import time
from dataclasses import dataclass

from src.components.registry import Registry

REGISTRY = Registry("VLM")


@dataclass
class VLMResult:
    text: str
    model: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    seconds: float | None = None


@REGISTRY.register("gemini", requires=["google.genai"], env=["GOOGLE_API_KEY", "GEMINI_API_KEY"],
                   note="google-genai SDK, 기본 gemini-2.5-flash")
def make_gemini(model=None):
    from google import genai
    from google.genai import types
    model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    client = genai.Client()

    def describe(png: bytes, prompt: str) -> VLMResult:
        t = time.perf_counter()
        r = client.models.generate_content(
            model=model, contents=[types.Part.from_bytes(data=png, mime_type="image/png"), prompt],
            config=types.GenerateContentConfig(temperature=0))
        u = r.usage_metadata
        return VLMResult((r.text or "").strip(), model, getattr(u, "prompt_token_count", None),
                         getattr(u, "candidates_token_count", None), round(time.perf_counter() - t, 2))
    return describe


@REGISTRY.register("openai", requires=["openai"], env=["OPENAI_API_KEY"], note="openai SDK, 기본 gpt-4.1-mini")
def make_openai(model=None):
    from openai import OpenAI
    model = model or os.getenv("OPENAI_VLM_MODEL", "gpt-4.1-mini")
    client = OpenAI()

    def describe(png: bytes, prompt: str) -> VLMResult:
        t = time.perf_counter()
        b64 = base64.b64encode(png).decode()
        r = client.chat.completions.create(model=model, temperature=0, messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}])
        return VLMResult(r.choices[0].message.content.strip(), model, r.usage.prompt_tokens,
                         r.usage.completion_tokens, round(time.perf_counter() - t, 2))
    return describe


@REGISTRY.register("claude", requires=["anthropic"], env=["ANTHROPIC_API_KEY"],
                   note="anthropic SDK, 기본 claude-haiku-5-5")
def make_claude(model=None):
    import anthropic
    model = model or os.getenv("CLAUDE_VLM_MODEL", "claude-haiku-5-5")
    client = anthropic.Anthropic()

    def describe(png: bytes, prompt: str) -> VLMResult:
        t = time.perf_counter()
        r = client.messages.create(model=model, max_tokens=2048, messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(png).decode()}},
            {"type": "text", "text": prompt}]}])
        text = "".join(b.text for b in r.content if b.type == "text").strip()
        return VLMResult(text, model, r.usage.input_tokens, r.usage.output_tokens, round(time.perf_counter() - t, 2))
    return describe
