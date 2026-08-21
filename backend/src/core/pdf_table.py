"""PDF 表格结构化提取（S5-B1/B2）：pdfplumber 页表格 → GFM Markdown。

（面向初学者的设计说明，按功能模块）

1. 本模块的位置：附件链路与入库链路共享的可选增强组件
   两条 PDF 链路（api/attachments.py 附件提取、core/knowledge/loaders.py
   教材入库）此前都用 pypdf 纯文本提取——表格被拍平成无结构的行文本，
   模型与检索都难以还原「这串数字是同一行的三个列」。本模块用
   pdfplumber 的表格探测把表格渲染成 GFM Markdown 附在页文本之后，
   让下游（模型上下文 / 分块检索）看到结构化的行列关系。

2. 选型与依赖口径
   pdfplumber：纯 Python（pdfminer.six 系）、Windows pip 可装、无原生
   依赖——作为可选依赖组 `pdf-table`（pyproject）提供，未安装时调用方
   按「可用才开」哲学降级为 pypdf 纯文本，行为与安装前逐项一致。

3. 降级语义（与 core/ocr.py 同一约定）
   - API_PDF_TABLE_MODE=off：强制关闭，不探测不构造；
   - auto（默认）：pdfplumber 未安装 / 打开失败 → None，调用方回退；
   - 其它值：ValueError（配置拼写错误要暴露，不静默当成 auto）。
   页级容错：单页表格探测/解析异常只丢弃该页表格小节（回退纯文本），
   不让整份 PDF 提取失败——表格是增强，不是正确性的前提。

4. 输出形态（v1 边界）
   每个表格渲染为「[表格] 小节标题 + GFM 表格」，附在该页文本之后；
   不做阅读顺序的图文混排还原（任务清单已明确的 v1 边界）。GFM 表格
   前端（remark-gfm）与模型均可直接消费。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

# 单页表格小节的标注头（任务清单 B1 规定的 v1 形态）。
_TABLE_SECTION_HEADER = "[表格]"


def _cell_text(cell: Any) -> str:
    """单元格归一：None → 空串、去首尾空白、竖线转义、换行折为空格。"""
    if cell is None:
        return ""
    text = str(cell).strip()
    # GFM 用竖线分列：内容里的竖线必须转义；单元格内换行会破坏行结构。
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _table_to_markdown(table: list[list[Any]]) -> str:
    """单个表格 → GFM Markdown（首行为表头）；空表返回空串。"""
    rows = [
        [_cell_text(cell) for cell in row]
        for row in table
        if row is not None and any(_cell_text(cell) for cell in row)
    ]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(rows[0]) + " |"]
    lines.append("| " + " | ".join(["---"] * width) + " |")
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


class PdfTableExtractor:
    """按页提取 PDF 表格并渲染为 Markdown 小节（需 with/close 管理）。"""

    def __init__(self, source: Path, _pdf: Any) -> None:
        self._source = source
        self._pdf = _pdf

    @classmethod
    def open(cls, source: Path) -> PdfTableExtractor:
        """打开 PDF；pdfplumber 缺失或文件不可读时抛错（由工厂降级）。"""
        try:
            import pdfplumber
        except ImportError as exc:
            raise RuntimeError(
                "PDF 表格提取需要 pdfplumber 包：请先运行 "
                "`uv sync --extra pdf-table`"
            ) from exc
        pdf = pdfplumber.open(str(source))
        return cls(source, pdf)

    def page_tables_markdown(self, page_number: int) -> str:
        """指定页（1 起）的全部表格 → 「[表格] + Markdown」小节文本。

        无表格返回空串；单页解析异常吞掉并返回空串（页级降级，见模块
        注释第 3 节）——调用方拿到的页文本至多缺表格增强，不会失败。
        """
        index = page_number - 1
        pages = getattr(self._pdf, "pages", [])
        if index < 0 or index >= len(pages):
            return ""
        try:
            tables = pages[index].extract_tables() or []
        except Exception:  # noqa: BLE001 - 页级降级：表格增强不允许破坏提取
            return ""
        sections = [
            markdown
            for table in tables
            if (markdown := _table_to_markdown(table))
        ]
        if not sections:
            return ""
        rendered = []
        for section in sections:
            rendered.append(f"{_TABLE_SECTION_HEADER}\n{section}")
        return "\n\n".join(rendered)

    def close(self) -> None:
        """释放底层文件句柄（pdfplumber 打开的文件需显式关闭）。"""
        self._pdf.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def open_pdf_table_extractor(
    source: Path,
    *,
    mode: str = "auto",
) -> PdfTableExtractor | None:
    """按模式打开表格提取器；不可用时返回 None（降级，不抛错）。

    模式语义与 create_ocr_provider 同一约定：
    - "auto"（默认）：尝试构造，pdfplumber 缺失 / 文件打开失败 → None；
    - "off"：强制关闭，不探测不构造；
    - 其它值：ValueError（配置错误要暴露，与 embedding/OCR 同一哲学）。
    """
    if mode == "off":
        return None
    if mode != "auto":
        raise ValueError("API_PDF_TABLE_MODE 只支持 auto 或 off")
    try:
        return PdfTableExtractor.open(source)
    except Exception:  # noqa: BLE001 - 可选能力探测，降级是设计意图
        # 未安装 pdf-table extra / 文件损坏不可读（pdfminer 对损坏文件
        # 抛出的异常类型无稳定基类）→ 降级纯文本，不阻断附件处理或
        # 入库流程；与 app.py _create_reranker 的宽捕获同一哲学。
        return None


__all__ = ["PdfTableExtractor", "open_pdf_table_extractor"]
