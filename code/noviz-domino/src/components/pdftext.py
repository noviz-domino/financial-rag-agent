"""PDF 페이지 텍스트 추출 어댑터. 모든 구현: extract(path) → list[str] (페이지 순서).

'로딩 방식 비교' 실험의 기준선용. 레이아웃 처리(펼침면·단·표)는 src/loader.py(pdfplumber 전용)가 담당하고,
여기서는 같은 PDF를 라이브러리만 바꿔 읽었을 때의 차이를 본다.
"""
from src.components.registry import Registry

REGISTRY = Registry("PDF text")


@REGISTRY.register("pdfplumber", requires=["pdfplumber"], note="좌표·폰트 정보 풍부, 느린 편")
def make_pdfplumber():
    import pdfplumber

    def extract(path):
        with pdfplumber.open(path) as pdf:
            return [p.extract_text() or "" for p in pdf.pages]
    return extract


@REGISTRY.register("pypdfium2", requires=["pypdfium2"], note="Chrome PDFium 엔진, 매우 빠름")
def make_pypdfium2():
    import pypdfium2 as pdfium

    def extract(path):
        doc = pdfium.PdfDocument(str(path))
        try:
            return [doc[i].get_textpage().get_text_range() for i in range(len(doc))]
        finally:
            doc.close()
    return extract


@REGISTRY.register("pypdf", requires=["pypdf"], note="순수 파이썬, 의존성 최소")
def make_pypdf():
    from pypdf import PdfReader

    def extract(path):
        return [p.extract_text() or "" for p in PdfReader(str(path)).pages]
    return extract


@REGISTRY.register("pymupdf", requires=["fitz"], note="빠르고 블록 정보 제공, AGPL 라이선스")
def make_pymupdf():
    import fitz

    def extract(path):
        with fitz.open(path) as doc:
            return [p.get_text() for p in doc]
    return extract
