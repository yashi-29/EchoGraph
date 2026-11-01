import io
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from xml.etree import ElementTree

from pypdf import PdfReader

# ~500 tokens per chunk: small enough that a search hit points at the relevant passage,
# large enough to keep a paragraph's context together. Consecutive chunks overlap slightly
# so a sentence cut at a boundary is still whole in one of them.
_CHUNK_CHARS = 2000
_CHUNK_OVERLAP = 200


class DocumentUnreadable(Exception):
    """The file can't be turned into text; the message is safe to show the user."""


@dataclass
class ExtractedText:
    text: str
    pages: int | None = None
    # From the PDF's own metadata — usually when the certificate/letter was produced.
    created_at: datetime | None = None


def extract_pdf(data: bytes) -> ExtractedText:
    try:
        reader = PdfReader(io.BytesIO(data))
        # Many "protected" PDFs only restrict printing/copying and open with an empty password.
        if reader.is_encrypted and not reader.decrypt(""):
            raise DocumentUnreadable("This PDF is password-protected.")
        pages = [page.extract_text() or "" for page in reader.pages]
    except DocumentUnreadable:
        raise
    except Exception as exc:  # pypdf raises a wide variety of errors on malformed files
        raise DocumentUnreadable("Couldn't read this PDF — the file may be damaged.") from exc

    created_at = None
    try:
        created_at = reader.metadata.creation_date if reader.metadata else None
    except Exception:  # malformed date strings in metadata are common and not worth failing on
        created_at = None
    if created_at is not None and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)

    return ExtractedText(text="\n\n".join(pages), pages=len(pages), created_at=created_at)


_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DOCX_XML_MAX_BYTES = 50 * 1024 * 1024


def extract_docx(data: bytes) -> ExtractedText:
    """Text of a Word .docx: its paragraphs (including table cells), one per line. A .docx is
    a zip of XML, so the standard library is enough — no python-docx dependency."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            # A small zip can inflate enormously; real documents' XML is a few MB at most.
            if archive.getinfo("word/document.xml").file_size > _DOCX_XML_MAX_BYTES:
                raise DocumentUnreadable("This Word document is too large to index.")
            root = ElementTree.fromstring(archive.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise DocumentUnreadable(
            "Couldn't read this Word document — the file may be damaged."
        ) from exc

    paragraphs = []
    for paragraph in root.iter(f"{_WORD_NS}p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == f"{_WORD_NS}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{_WORD_NS}tab":
                parts.append("\t")
            elif node.tag in (f"{_WORD_NS}br", f"{_WORD_NS}cr"):
                parts.append("\n")
        paragraphs.append("".join(parts))
    # Blank paragraphs are Word's spacing; real ones keep their own line so chunking can
    # still find paragraph boundaries.
    return ExtractedText(text="\n\n".join(p for p in paragraphs if p.strip()))


def decode_text(data: bytes) -> ExtractedText:
    if b"\x00" in data:
        raise DocumentUnreadable("This doesn't look like a text file.")
    try:
        return ExtractedText(text=data.decode("utf-8-sig"))
    except UnicodeDecodeError:
        # Older Windows editors save as cp1252 rather than UTF-8.
        return ExtractedText(text=data.decode("cp1252", errors="replace"))


def _split_long(paragraph: str) -> list[str]:
    pieces = []
    while len(paragraph) > _CHUNK_CHARS:
        cut = max(paragraph.rfind(" ", 0, _CHUNK_CHARS), paragraph.rfind("\n", 0, _CHUNK_CHARS))
        if cut < _CHUNK_CHARS // 2:  # no whitespace near the limit (e.g. a long URL)
            cut = _CHUNK_CHARS
        pieces.append(paragraph[:cut].strip())
        paragraph = paragraph[cut:].strip()
    if paragraph:
        pieces.append(paragraph)
    return pieces


def chunk_text(text: str) -> list[str]:
    """Splits text into ~_CHUNK_CHARS chunks along paragraph boundaries where possible."""
    text = text.replace("\x00", "")  # Postgres text columns reject NUL bytes
    pieces: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = re.sub(r"[ \t]+", " ", paragraph).strip()
        if paragraph:
            pieces.extend(_split_long(paragraph))

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + 2 + len(piece) > _CHUNK_CHARS:
            chunks.append(current)
            tail = current[-_CHUNK_OVERLAP:]
            # Start the overlap on a word boundary rather than mid-word.
            tail = tail[tail.find(" ") + 1 :] if " " in tail else tail
            current = f"{tail}\n\n{piece}"
        else:
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks
