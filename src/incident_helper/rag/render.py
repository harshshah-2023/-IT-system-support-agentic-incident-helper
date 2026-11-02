"""Renders the Markdown runbook source to the PDF and HTML that the RAG pipelines ingest.

The PDF carries real outline bookmarks, a generated contents page and page numbers, the same structure
a reviewer-assigned PDF would be expected to have. `incident render-runbook` runs this.
"""

from __future__ import annotations

import re
from pathlib import Path
from xml.sax.saxutils import escape

import markdown
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

_BASE = getSampleStyleSheet()
BODY = ParagraphStyle(
    "Body", parent=_BASE["BodyText"], fontName="Helvetica", fontSize=10.5, leading=15, spaceAfter=6
)
CELL = ParagraphStyle("Cell", parent=BODY, fontSize=9, leading=12, spaceAfter=0)
CODE = ParagraphStyle(
    "Code", parent=BODY, fontName="Courier", fontSize=9, leading=12, backColor=colors.whitesmoke
)
HEADINGS = {
    1: ParagraphStyle(
        "H1", parent=BODY, fontName="Helvetica-Bold", fontSize=20, leading=25, spaceBefore=10, spaceAfter=10
    ),
    2: ParagraphStyle(
        "H2", parent=BODY, fontName="Helvetica-Bold", fontSize=14, leading=18, spaceBefore=14, spaceAfter=6
    ),
    3: ParagraphStyle(
        "H3", parent=BODY, fontName="Helvetica-Bold", fontSize=11.5, leading=15, spaceBefore=8, spaceAfter=4
    ),
}


def _inline(text: str) -> str:
    text = escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`([^`]+)`", r'<font face="Courier">\1</font>', text)
    return text


class _RunbookDoc(BaseDocTemplate):
    def __init__(self, path: str, title: str) -> None:
        super().__init__(
            path,
            pagesize=A4,
            title=title,
            leftMargin=22 * mm,
            rightMargin=22 * mm,
            topMargin=20 * mm,
            bottomMargin=20 * mm,
        )
        frame = Frame(self.leftMargin, self.bottomMargin, self.width, self.height, id="body")
        self.addPageTemplates([PageTemplate(id="page", frames=[frame], onPage=self._footer)])
        self._title = title

    def _footer(self, canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.grey)
        canvas.drawString(self.leftMargin, 12 * mm, "CONFIDENTIAL - INTERNAL USE ONLY")
        canvas.drawRightString(A4[0] - self.rightMargin, 12 * mm, f"Page {doc.page}")
        canvas.restoreState()

    def afterFlowable(self, flowable) -> None:
        level = getattr(flowable, "_heading_level", None)
        if level is None:
            return
        title = flowable.getPlainText()
        key = f"h{id(flowable)}"
        self.canv.bookmarkPage(key)
        self.canv.addOutlineEntry(title, key, level=level, closed=level > 0)
        if level < 2:
            self.notify("TOCEntry", (level, title, self.page, key))


def _flowables(source: str) -> list:
    lines = source.splitlines()
    story: list = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            story.append(Paragraph(_inline(" ".join(paragraph)), BODY))
            paragraph.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        heading = re.match(r"^(#{2,4})\s+(.*)$", stripped)
        if heading:
            flush()
            level = len(heading[1]) - 2
            para = Paragraph(_inline(heading[2]), HEADINGS[level + 1])
            para._heading_level = level
            if level == 0 and story:
                story.append(PageBreak())
            story.append(para)
        elif stripped.startswith("```"):
            flush()
            block = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            story.append(Preformatted("\n".join(block), CODE))
            story.append(Spacer(1, 6))
        elif stripped.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    rows.append([Paragraph(_inline(c), CELL) for c in cells])
                i += 1
            i -= 1
            table = Table(
                rows, repeatRows=1, hAlign="LEFT", colWidths=[166 * mm / len(rows[0])] * len(rows[0])
            )
            table.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.4, colors.lightgrey),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            story.extend([table, Spacer(1, 8)])
        elif re.match(r"^(\d+\.|[-*])\s+", stripped):
            flush()
            story.append(
                Paragraph(_inline(stripped), ParagraphStyle("Li", parent=BODY, leftIndent=14, spaceAfter=3))
            )
        elif not stripped:
            flush()
        else:
            paragraph.append(stripped)
        i += 1
    flush()
    return story


def render_pdf(markdown_path: Path, pdf_path: Path) -> int:
    source = markdown_path.read_text(encoding="utf-8")
    title = source.splitlines()[0].lstrip("# ").strip()
    body = "\n".join(source.splitlines()[1:])
    toc = TableOfContents()
    toc.levelStyles = [
        ParagraphStyle("Toc0", parent=BODY, fontName="Helvetica-Bold", spaceAfter=2),
        ParagraphStyle("Toc1", parent=BODY, leftIndent=14, spaceAfter=0, fontSize=9.5, leading=12),
    ]
    story = [
        Paragraph(_inline(title), HEADINGS[1]),
        Paragraph("Contents", HEADINGS[2]),
        toc,
        PageBreak(),
        *_flowables(body),
    ]
    doc = _RunbookDoc(str(pdf_path), title)
    doc.multiBuild(story)
    return doc.page


def render_html(markdown_path: Path, html_path: Path) -> None:
    source = markdown_path.read_text(encoding="utf-8")
    body = markdown.markdown(source, extensions=["tables", "fenced_code"])
    html_path.write_text(
        f"<!doctype html><html><head><meta charset='utf-8'></head><body>{body}</body></html>",
        encoding="utf-8",
    )
