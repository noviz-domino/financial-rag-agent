"""OCR 엔진 어댑터. 모든 엔진은 numpy RGB 이미지 → list[OCRLine] (위→아래, 왼→오른쪽 정렬)."""
from dataclasses import dataclass

import numpy as np

from src.components.registry import Registry

REGISTRY = Registry("OCR")


@dataclass
class OCRLine:
    text: str
    box: tuple          # (x0, y0, x1, y1) 픽셀
    conf: float | None = None


def sort_lines(lines):
    return sorted(lines, key=lambda l: (round(l.box[1] / 10), l.box[0]))


def to_text(lines):
    return "\n".join(l.text for l in lines)


def _bbox(points):
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


@REGISTRY.register("easyocr", requires=["easyocr"], note="PyTorch 기반, ko+en, CPU에서 느린 편")
def make_easyocr(langs=("ko", "en"), gpu=False):
    import easyocr
    reader = easyocr.Reader(list(langs), gpu=gpu, verbose=False)

    def run(img: np.ndarray):
        return sort_lines([OCRLine(t, _bbox(b), float(c)) for b, t, c in reader.readtext(img, detail=1)])
    return run


@REGISTRY.register("winocr", requires=["winocr"], note="Windows 내장 OCR(Windows.Media.Ocr), 한국어 언어팩 필요")
def make_winocr(lang="ko"):
    import winocr
    from PIL import Image

    def run(img: np.ndarray):
        res = winocr.recognize_pil_sync(Image.fromarray(img), lang)
        out = []
        for line in res.get("lines", []):
            rects = [w["bounding_rect"] for w in line.get("words", [])]
            if rects:
                box = (min(r["x"] for r in rects), min(r["y"] for r in rects),
                       max(r["x"] + r["width"] for r in rects), max(r["y"] + r["height"] for r in rects))
            else:
                box = (0, 0, 0, 0)
            out.append(OCRLine(line["text"], box, None))
        return sort_lines(out)
    return run


@REGISTRY.register("rapidocr", requires=["rapidocr", "shapely"], note="ONNX 경량, PP-OCRv5 한국어 모델")
def make_rapidocr():
    from rapidocr import LangRec, OCRVersion, RapidOCR
    eng = RapidOCR(params={"Rec.lang_type": LangRec.KOREAN, "Rec.ocr_version": OCRVersion.PPOCRV5})

    def run(img: np.ndarray):
        r = eng(img)
        if r is None or r.txts is None:
            return []
        return sort_lines([OCRLine(t, _bbox(b), float(s)) for b, t, s in zip(r.boxes, r.txts, r.scores)])
    return run


@REGISTRY.register("tesseract", requires=["pytesseract"], note="Tesseract 프로그램 + kor 언어 데이터 별도 설치 필요")
def make_tesseract(lang="kor+eng"):
    import pytesseract
    from PIL import Image

    def run(img: np.ndarray):
        d = pytesseract.image_to_data(Image.fromarray(img), lang=lang, output_type=pytesseract.Output.DICT)
        lines = {}
        for i, t in enumerate(d["text"]):
            if t.strip():
                key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
                lines.setdefault(key, []).append(i)
        out = []
        for idx in lines.values():
            x0 = min(d["left"][i] for i in idx)
            y0 = min(d["top"][i] for i in idx)
            x1 = max(d["left"][i] + d["width"][i] for i in idx)
            y1 = max(d["top"][i] + d["height"][i] for i in idx)
            out.append(OCRLine(" ".join(d["text"][i] for i in idx), (x0, y0, x1, y1), None))
        return sort_lines(out)
    return run
