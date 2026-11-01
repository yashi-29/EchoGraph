import io
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from xml.etree import ElementTree

import pillow_heif
import pytesseract
from PIL import Image, ImageOps

from app.core.config import get_settings

settings = get_settings()

# Lets Pillow open iPhone photos (.heic/.heif); every other format here is built in.
pillow_heif.register_heif_opener()

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".jfif",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".avif",
    ".ico",
    ".svg",
}

# A 48 MP phone photo is ~48M pixels; anything far beyond that is a decompression bomb
# (a tiny file that inflates to gigabytes in memory), not a real picture.
_MAX_PIXELS = 100_000_000
# Tesseract reads text best at ~300 DPI: small screenshots are upscaled, huge photos are
# shrunk so OCR stays within the request.
_MIN_OCR_SIDE = 1000
_MAX_OCR_SIDE = 4000
# Multi-page TIFFs are usually scanned documents; beyond this only the start is read.
_MAX_FRAMES = 20
_OCR_TIMEOUT_SECONDS = 60

_EXIF_IFD = 0x8769
_DATETIME = 306
_MAKE = 271
_MODEL = 272
_DATETIME_ORIGINAL = 36867
_OFFSET_TIME_ORIGINAL = 36881

_SVG_TEXT_TAGS = {"text", "tspan", "title", "desc"}


class ImageUnreadable(Exception):
    """The file can't be opened as an image; the message is safe to show the user."""


class OcrFailed(Exception):
    """Tesseract itself failed (missing binary, timeout) — not the user's file."""


@dataclass
class ExtractedImage:
    text: str
    width: int | None = None
    height: int | None = None
    frames: int = 1
    # From EXIF — when the photo was taken, not when it was copied or downloaded.
    taken_at: datetime | None = None
    camera: str | None = None


def _clean_ocr(raw: str) -> str:
    """Drops the stray-symbol lines Tesseract produces on photos with little or no text."""
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if re.search(r"[^\W_]{2,}", line):
            lines.append(line)
        elif not line and lines and lines[-1]:
            lines.append("")  # keep paragraph breaks for chunking
    return "\n".join(lines).strip()


def _ocr(frame: Image.Image) -> str:
    frame = ImageOps.exif_transpose(frame).convert("L")
    longest = max(frame.size)
    if longest < _MIN_OCR_SIDE:
        scale = min(2.0, _MIN_OCR_SIDE / longest)
    elif longest > _MAX_OCR_SIDE:
        scale = _MAX_OCR_SIDE / longest
    else:
        scale = 1.0
    if scale != 1.0:
        size = (max(1, round(frame.width * scale)), max(1, round(frame.height * scale)))
        frame = frame.resize(size, Image.Resampling.LANCZOS)
    try:
        raw = pytesseract.image_to_string(
            frame, lang=settings.ocr_languages, timeout=_OCR_TIMEOUT_SECONDS
        )
    except (pytesseract.TesseractNotFoundError, pytesseract.TesseractError, RuntimeError) as exc:
        # RuntimeError is how pytesseract reports its timeout.
        raise OcrFailed(str(exc)) from exc
    return _clean_ocr(str(raw))


def _exif_datetime(value: object, offset: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        taken = datetime.strptime(value.strip("\x00 "), "%Y:%m:%d %H:%M:%S")
    except ValueError:  # cameras with no clock set write "0000:00:00 00:00:00"
        return None
    match = None
    if isinstance(offset, str):
        match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", offset.strip("\x00 "))
    if match:
        sign = 1 if match[1] == "+" else -1
        delta = timedelta(hours=int(match[2]), minutes=int(match[3]))
        return taken.replace(tzinfo=timezone(sign * delta))
    # No offset recorded (most cameras before ~2018): the wall-clock time is all there is.
    return taken.replace(tzinfo=UTC)


def _exif_facts(image: Image.Image) -> tuple[datetime | None, str | None]:
    try:
        exif = image.getexif()
        detail = exif.get_ifd(_EXIF_IFD)
    except Exception:  # malformed EXIF is common and never worth failing the upload over
        return None, None
    taken_at = _exif_datetime(
        detail.get(_DATETIME_ORIGINAL), detail.get(_OFFSET_TIME_ORIGINAL)
    ) or _exif_datetime(exif.get(_DATETIME), None)

    make = str(exif.get(_MAKE) or "").strip("\x00 ")
    model = str(exif.get(_MODEL) or "").strip("\x00 ")
    # Some models already include the make ("Canon" + "Canon EOS R6"), others don't
    # ("Apple" + "iPhone 15") — avoid "Canon Canon EOS R6".
    camera = model if make and model.lower().startswith(make.lower()) else f"{make} {model}"
    return taken_at, camera.strip() or None


def extract_image(data: bytes) -> ExtractedImage:
    """OCR text plus EXIF facts of a raster image. CPU-bound — run it in a thread."""
    try:
        image = Image.open(io.BytesIO(data))
    # Beyond UnidentifiedImageError, format plugins raise their own errors on truncated files.
    except Exception as exc:
        raise ImageUnreadable("Couldn't read this image — the file may be damaged.") from exc

    with image:
        # Image.open only reads the header, so this check runs before any pixels are decoded.
        if image.width * image.height > _MAX_PIXELS:
            raise ImageUnreadable("This image's dimensions are too large to read.")
        taken_at, camera = _exif_facts(image)
        # Every page of a scanned TIFF is a page of the document; the other frames of an
        # animated GIF/WebP are just the same picture again.
        frames = min(getattr(image, "n_frames", 1), _MAX_FRAMES) if image.format == "TIFF" else 1
        pages = []
        try:
            for index in range(frames):
                image.seek(index)
                pages.append(_ocr(image))
        except OcrFailed:
            raise
        except Exception as exc:  # truncated pixel data only surfaces when decoding
            raise ImageUnreadable("Couldn't read this image — the file may be damaged.") from exc

        return ExtractedImage(
            text="\n\n".join(page for page in pages if page),
            width=image.width,
            height=image.height,
            frames=frames,
            taken_at=taken_at,
            camera=camera,
        )


def extract_svg(data: bytes) -> ExtractedImage:
    """An SVG is XML: its visible text is in <text>/<tspan>, plus an optional <title> and
    <desc>. Nothing is rendered or executed."""
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise ImageUnreadable("Couldn't read this SVG — the file may be damaged.") from exc
    parts = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1] if isinstance(node.tag, str) else ""
        if tag in _SVG_TEXT_TAGS and node.text and node.text.strip():
            parts.append(node.text.strip())
    return ExtractedImage(text="\n".join(parts))


def describe(extracted: ExtractedImage) -> str:
    """The memory's body: a facts line that makes the image findable even when it has no
    text ("photos from my iPhone in May"), then whatever text was read off it."""
    facts = ["Image"]
    if extracted.width and extracted.height:
        facts.append(f"{extracted.width}×{extracted.height}")
    if extracted.frames > 1:
        facts.append(f"{extracted.frames} pages")
    if extracted.taken_at:
        facts.append(f"taken {extracted.taken_at:%d %B %Y}")
    if extracted.camera:
        facts.append(extracted.camera)
    header = " · ".join(facts)
    if extracted.text:
        return f"{header}\n\nText in the image:\n{extracted.text}"
    return f"{header}\n\nNo readable text was found in this image."
