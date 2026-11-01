from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser

# Chrome, Edge, Firefox and Safari all export bookmarks in this (Netscape) format.
NETSCAPE_MARKER = b"NETSCAPE-Bookmark-file"


@dataclass
class Bookmark:
    url: str
    title: str
    folder: str
    added_at: datetime | None


def _parse_add_date(value: str | None) -> datetime | None:
    try:
        timestamp = float(value or "")
    except ValueError:
        return None
    # Normally Unix seconds, but some exporters write milliseconds or microseconds.
    while timestamp > 1e11:
        timestamp /= 1000
    if timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


class _BookmarkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.bookmarks: list[Bookmark] = []
        self._folders: list[str | None] = []  # one entry per open <DL>
        self._pending_folder: str | None = None  # last <H3> seen, names the next <DL>
        self._text: list[str] | None = None
        self._link: dict[str, str | None] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "dl":
            self._folders.append(self._pending_folder)
            self._pending_folder = None
        elif tag == "h3":
            self._text = []
        elif tag == "a":
            self._link = dict(attrs)
            self._text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "dl" and self._folders:
            self._folders.pop()
        elif tag == "h3" and self._text is not None:
            self._pending_folder = "".join(self._text).strip() or None
            self._text = None
        elif tag == "a" and self._link is not None:
            url = (self._link.get("href") or "").strip()
            # Skip bookmarklets and browser-internal pages (javascript:, place:, chrome://).
            if url.startswith(("http://", "https://")):
                self.bookmarks.append(
                    Bookmark(
                        url=url,
                        title="".join(self._text or []).strip() or url,
                        folder=" / ".join(f for f in self._folders if f),
                        added_at=_parse_add_date(self._link.get("add_date")),
                    )
                )
            self._link = None
            self._text = None

    def handle_data(self, data: str) -> None:
        if self._text is not None:
            self._text.append(data)


def parse_bookmarks(html: str) -> list[Bookmark]:
    """Every http(s) bookmark in a Netscape bookmark export, first occurrence of each URL."""
    parser = _BookmarkParser()
    parser.feed(html)
    parser.close()
    seen: set[str] = set()
    unique = []
    for bookmark in parser.bookmarks:
        if bookmark.url not in seen:
            seen.add(bookmark.url)
            unique.append(bookmark)
    return unique
