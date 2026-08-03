import pdf_inspector

result = pdf_inspector.process_pdf("D:/Users/Kevin/Downloads/Mobile Devices/扫描文档20260803_093318.pdf")
print(result.pdf_type)   # "text_based", "scanned", "image_based", "mixed"
print(result.markdown)   # Markdown string or None
print(f"需要OCR的页码: {result.pages_needing_ocr}")