"""Parse local text documents into bounded, source-addressable chunks."""

import re
from bisect import bisect_right
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.tools.list_files import resolve_repository_path


@dataclass(frozen=True)
class PageSpan:
    number: int
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class Document:
    workspace_id: str
    document_id: str
    version: str
    source: str
    text: str
    pages: tuple[PageSpan, ...] = ()

    @property
    def pages_without_text(self) -> list[int]:
        return [
            page.number
            for page in self.pages
            if not self.text[page.start_offset : page.end_offset].strip()
        ]


@dataclass(frozen=True)
class Chunk:
    workspace_id: str
    document_id: str
    version: str
    chunk_id: str
    source: str
    heading: str | None
    start_line: int
    end_line: int
    start_offset: int
    end_offset: int
    text: str
    page: int | None = None

    @property
    def citation(self) -> str:
        if self.page is not None:
            return f"{self.source}:p{self.page}"
        return f"{self.source}:{self.start_line}-{self.end_line}"


def _extract_pdf(raw: bytes) -> tuple[str, tuple[PageSpan, ...]]:
    try:
        reader = PdfReader(BytesIO(raw))
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs are not supported")
        texts = [page.extract_text() or "" for page in reader.pages]
    except PdfReadError as exc:
        raise ValueError(f"Cannot parse PDF: {exc}") from exc
    if not any(text.strip() for text in texts):
        raise ValueError(
            "PDF has no extractable text: it may be blank or image-only; OCR is not supported"
        )
    pages, offset = [], 0
    for number, text in enumerate(texts, 1):
        pages.append(PageSpan(number, offset, offset + len(text)))
        offset += len(text) + 2
    return "\n\n".join(texts), tuple(pages)


def parse_document(workspace: Path, path: str) -> Document:
    """Read UTF-8 Markdown/TXT or text-layer PDF within the existing path policy."""
    root = workspace.resolve(strict=True)
    file = resolve_repository_path(root, path)
    if file.suffix.lower() not in {".md", ".txt", ".pdf"}:
        raise ValueError("Only Markdown (.md), TXT (.txt) and PDF (.pdf) are supported")
    raw = file.read_bytes()
    source = file.relative_to(root).as_posix()
    workspace_id = sha256(str(root).encode("utf-8")).hexdigest()
    document_id = sha256(f"{workspace_id}/{source}".encode("utf-8")).hexdigest()
    text, pages = (
        _extract_pdf(raw) if file.suffix.lower() == ".pdf" else (raw.decode("utf-8-sig"), ())
    )
    return Document(workspace_id, document_id, sha256(raw).hexdigest(), source, text, pages)


def _boundaries(text: str, markdown: bool):
    sections = [(0, None)]
    paragraphs = []
    line_ends = []
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n")) if markdown else None
        if marker:
            run, tail = marker.groups()
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not tail.strip():
                fence = None
        elif fence is None:
            heading = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*$", line) if markdown else None
            if heading:
                sections.append((offset, heading.group(1)))
            if not line.strip():
                paragraphs.append(offset + len(line))
        offset += len(line)
        line_ends.append(offset)
    return sections, paragraphs, line_ends


def chunk_document(
    document: Document, max_chars: int = 1000, overlap_chars: int = 0
) -> list[Chunk]:
    """Prefer headings/paragraphs, then lines; overlap stays within a section/page.

    Offsets are zero-based Python character positions, with an exclusive end.
    Line numbers are one-based and inclusive in extracted text; PDF citations use physical
    pages instead. PDF chunks never cross a page. Neither offsets nor size count bytes/tokens.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    if not 0 <= overlap_chars < max_chars:
        raise ValueError("overlap_chars must be between zero and max_chars - 1")
    text = document.text
    sections, paragraphs, line_ends = _boundaries(text, document.source.lower().endswith(".md"))
    line_starts = [0] + line_ends[:-1]
    spans = [
        (start, heading, sections[index + 1][0] if index + 1 < len(sections) else len(text), None)
        for index, (start, heading) in enumerate(sections)
    ]
    if document.pages:
        spans = [(page.start_offset, None, page.end_offset, page.number) for page in document.pages]
    chunks = []
    for start, heading, end, page_number in spans:
        while start < end:
            stop = min(start + max_chars, end)
            if stop < end:
                for boundaries in (paragraphs, line_ends):
                    position = bisect_right(boundaries, stop) - 1
                    if position >= 0 and boundaries[position] > start + overlap_chars:
                        stop = boundaries[position]
                        break
                else:
                    space = text.rfind(" ", start, stop)
                    if space > start + overlap_chars:
                        stop = space + 1
                # Keep Windows CRLF pairs together when the size permits it.
                if stop > start + 1 and text[stop - 1 : stop + 1] == "\r\n":
                    stop -= 1
            content = text[start:stop]
            if content.strip():
                chunk_id = sha256(
                    f"{document.document_id}/{document.version}/{start}:{stop}".encode("utf-8")
                ).hexdigest()
                chunks.append(
                    Chunk(
                        document.workspace_id,
                        document.document_id,
                        document.version,
                        chunk_id,
                        document.source,
                        heading,
                        bisect_right(line_starts, start),
                        bisect_right(line_starts, stop - 1),
                        start,
                        stop,
                        content,
                        page_number,
                    )
                )
            if stop == end:
                break
            start = max(start + 1, stop - overlap_chars)
    return chunks
