"""OCR 엔진 어댑터 (설정 `figures.ocr_engine`으로 교체). 모두 png bytes → 텍스트.

엔진: winocr(기본, Windows 내장 OCR) · easyocr · none(아무것도 안 함).
엔진이 없거나(윈도우가 아님·미설치) 실패하면 no-op 으로 내려가서 파이프라인은 계속 돈다 (그림 속 글자 라우팅만 약해짐).
"""
from __future__ import annotations

import io
import warnings


def noop(png: bytes) -> str:
    return ""


noop.available = False


def _scaled(png: bytes, scale: float):
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGB")
    if scale != 1:
        im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
    return im


def _winocr(scale, lang="ko"):
    import winocr

    def run(png: bytes) -> str:
        res = winocr.recognize_pil_sync(_scaled(png, scale), lang)
        return "\n".join(ln["text"] for ln in res.get("lines", []))
    return run


def _easyocr(scale, langs=("ko", "en")):
    import easyocr
    import numpy as np
    reader = easyocr.Reader(list(langs), gpu=False, verbose=False)

    def run(png: bytes) -> str:
        res = reader.readtext(np.array(_scaled(png, scale)), detail=0)
        return "\n".join(res)
    return run


ENGINES = {"winocr": _winocr, "easyocr": _easyocr, "none": lambda scale: noop}


def make_ocr(engine: str, scale: float = 2.0):
    """반환: (함수 png→텍스트, 실제 쓰는 엔진 이름). 못 쓰면 ('none', no-op)."""
    if engine not in ENGINES:
        raise ValueError(f"알 수 없는 OCR 엔진: {engine} (가능: {list(ENGINES)})")
    try:
        fn = ENGINES[engine](scale)
        if engine == "winocr":   # 언어팩이 없으면 첫 호출에서 실패하므로 빈 이미지로 미리 확인
            from PIL import Image
            buf = io.BytesIO()
            Image.new("RGB", (40, 20), "white").save(buf, format="PNG")
            fn(buf.getvalue())
        return fn, engine
    except Exception as e:  # noqa: BLE001
        warnings.warn(f"OCR 엔진 {engine} 을 쓸 수 없어 no-op 으로 대체해요: {e}")
        return noop, "none"
