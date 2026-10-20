from src.components import ocr, pdftext, vlm

for mod in (ocr, vlm, pdftext):
    print(mod.REGISTRY.report())
