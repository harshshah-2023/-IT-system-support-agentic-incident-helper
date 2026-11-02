"""Format-independent document model: PDF, HTML and Markdown all parse to the same section list.

Both retrieval modules are built from `parse_document`, which is what guarantees they use the same
source. Swapping in a reviewer's document means pointing RUNBOOK_PATH at it and re-indexing.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup
from langchain_core.documents import Document
from pypdf import PdfReader

_NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+\S")
_FOOTER = re.compile(r"CONFIDENTIAL - INTERNAL USE ONLY\s*|Page \d+\s*$", re.MULTILINE)


@dataclass(slots=True)
class Section:
    title: str
    level: int
    text: str = ""
    page_start: int = 1
    page_end: int = 1
    # (character offset in `text`, page number), ascending; lets a chunk recover its exact page.
    page_marks: list[tuple[int, int]] = field(default_factory=list)

    def page_at(self, offset: int) -> int:
        page = self.page_start
        for start, number in self.page_marks:
            if start > offset:
                break
            page = number
        return page


def _flatten_outline(reader: PdfReader, items, level: int = 0) -> list[tuple[int, str, int]]:
    flat: list[tuple[int, str, int]] = []
    for item in items:
        if isinstance(item, list):
            flat += _flatten_outline(reader, item, level + 1)
        else:
            flat.append((level, str(item.title), reader.get_destination_page_number(item) + 1))
    return flat


def _parse_pdf(path: Path) -> list[Section]:
    reader = PdfReader(str(path))
    pages = [_FOOTER.sub("", page.extract_text() or "") for page in reader.pages]
    outline = _flatten_outline(reader, reader.outline)
    if not outline:
        return _sections_from_numbered_lines(pages)

    sections: list[Section] = []
    for index, (level, title, page) in enumerate(outline):
        next_page = outline[index + 1][2] if index + 1 < len(outline) else len(pages)
        next_title = outline[index + 1][1] if index + 1 < len(outline) else None
        section = Section(title=title, level=level + 1, page_start=page, page_end=next_page)
        buffer = ""
        for number in range(page, next_page + 1):
            text = pages[number - 1]
            if number == page:
                position = text.find(title)
                text = text[position + len(title) :] if position >= 0 else text
            if number == next_page and next_title is not None:
                position = text.find(next_title)
                if position >= 0:
                    text = text[:position]
                elif next_page != page:
                    continue
            if text.strip():
                section.page_marks.append((len(buffer), number))
            buffer += text.strip() + "\n"
        section.text = buffer.strip()
        section.page_end = section.page_marks[-1][1] if section.page_marks else page
        sections.append(section)
    return sections


def _sections_from_numbered_lines(pages: list[str]) -> list[Section]:
    """Fallback for PDFs without bookmarks: use numbered headings such as `4.2 Rollback`."""
    sections: list[Section] = []
    current: Section | None = None
    for number, text in enumerate(pages, start=1):
        for line in text.splitlines():
            match = _NUMBERED.match(line.strip())
            if match and len(line) < 80:
                current = Section(
                    title=line.strip(), level=match[1].count(".") + 1, page_start=number, page_end=number
                )
                sections.append(current)
            elif current is not None:
                if not current.page_marks or current.page_marks[-1][1] != number:
                    current.page_marks.append((len(current.text), number))
                current.text += line + "\n"
                current.page_end = number
    return sections


def _parse_markdown(path: Path) -> list[Section]:
    sections: list[Section] = []
    current: Section | None = None
    in_code = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        heading = None if in_code else re.match(r"^(#{2,6})\s+(.*)$", line)
        if heading:
            current = Section(title=heading[2].strip(), level=len(heading[1]) - 1)
            sections.append(current)
        elif current is not None:
            current.text += line + "\n"
    for section in sections:
        section.text = section.text.strip()
    return sections


def _parse_html(path: Path) -> list[Section]:
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    sections: list[Section] = []
    current: Section | None = None
    for element in soup.find_all(["h2", "h3", "h4", "h5", "p", "li", "table", "pre"]):
        if element.name.startswith("h"):
            current = Section(title=element.get_text(strip=True), level=int(element.name[1]) - 1)
            sections.append(current)
        elif current is not None and not element.find_parent(["table", "li"]):
            current.text += element.get_text(" ", strip=True) + "\n"
    return sections


PARSERS = {
    ".pdf": _parse_pdf,
    ".md": _parse_markdown,
    ".markdown": _parse_markdown,
    ".html": _parse_html,
    ".htm": _parse_html,
}


def parse_document(path: Path) -> list[Section]:
    parser = PARSERS.get(path.suffix.lower())
    if parser is None:
        raise ValueError(f"Unsupported document type {path.suffix!r}; expected one of {sorted(PARSERS)}")
    sections = parser(path)
    if not sections:
        raise ValueError(f"No sections found in {path}")
    return sections


def to_documents(sections: list[Section], source: str) -> list[Document]:
    return [
        Document(
            page_content=section.text,
            metadata={
                "source": source,
                "section": section.title,
                "page": section.page_start,
                "section_index": index,
            },
        )
        for index, section in enumerate(sections)
        if section.text
    ]


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
