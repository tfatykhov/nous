"""F112 MCP client: an image from a tool's result, made into one the model can be given.

A server's image is never passed on as the server wrote it. It is decoded here and encoded again, so the model is
handed an image this process made:

- one the API can read. A damaged image, or one whose bytes are not the type it declares, makes the API refuse the
  whole request (400), which would end the turn; an image that decoded here cannot be either;
- the pixels and nothing beside them. The encoders are given a new image built from the decoded pixels alone, so
  nothing the source carried beside them (text chunks, a comment, a colour profile, EXIF, a second frame, bytes
  after the image's end) has anything to be written from;
- no longer than ``LONG_EDGE`` pixels a side, so its cost in tokens is bounded and known.

What decoding costs is bounded before a pixel is decoded, by what the bytes say of themselves: the size of the
source, the pixel count in its header, and for a JPEG the number of its scans (each is a pass over the whole image,
and a few dozen bytes can ask for one).

Only PNG and JPEG are read, and only by their own two decoders. Pillow does the decoding; without it every image
stays a line of text.

Nothing here raises: an image that cannot be used is answered with why, in this module's words, never the server's.
Decoding is work for a thread, never the event loop: a few kilobytes can unpack to tens of megabytes of pixels.
"""

from __future__ import annotations

import base64
import binascii
import io
from dataclasses import dataclass
from typing import Any

# What a server may send, by the media type it declares: the decoder that reads it.
IMAGE_TYPES = {"image/png": "PNG", "image/jpeg": "JPEG"}
# The decoded size of what the server sent. The client's response limit (NOUS_MCP_CLIENT_MAX_RESPONSE_BYTES) is the
# smaller bound by default; this one holds when that limit is raised.
SOURCE_MAX_BYTES = 5_000_000
# Width times height, read from the header before a pixel is decoded: an image that would unpack to more is refused
# by its header alone. 4096 by 4096 holds two 4K screens side by side. One call on a PNG of that size with an alpha
# channel was measured at about 170 MB over what the process held before (64 MB of pixels, the copy the scaling makes
# of them, and its buffers); a JPEG of that size is decoded at half size and adds little. The manager runs no more
# than two decodes at a time.
PIXELS_MAX = 4096 * 4096
# The scans of a JPEG: a baseline one has 1, a progressive one about 10. Each scan is a pass over every block of the
# image, so a file that repeats a 56-byte scan thousands of times decodes for minutes. The count is of the scan
# marker's two bytes anywhere in the file, which is never less than the scans a decoder will find (image data cannot
# hold them); what else can hold them is metadata, a thumbnail's own scan for one.
JPEG_SCANS_MAX = 64
_JPEG_SCAN = bytes((0xFF, 0xDA))  # the start-of-scan marker
# The longer side of what is sent. The API scales a larger image down to about this itself.
LONG_EDGE = 1568
# The encoded size of what is sent. A PNG over it is sent as a JPEG; a JPEG over it is not sent.
OUT_MAX_BYTES = 1_000_000


@dataclass(frozen=True)
class OutsideImage:
    """An image as it is sent: encoded here, from pixels decoded here."""

    media_type: str
    data: str  # base64 of the bytes encoded here
    width: int
    height: int

    def block(self) -> dict[str, Any]:
        """The image as a content block of a ``tool_result``."""
        return {"type": "image", "source": {"type": "base64", "media_type": self.media_type, "data": self.data}}

    def line(self) -> str:
        """The image in words, for the result's text (the text is what the logs and the ledger keep)."""
        return f"[image: {IMAGE_TYPES[self.media_type]} {self.width}x{self.height}]"


def prepare_image(data: Any, media_type: Any) -> OutsideImage | str:
    """``data`` (base64, as a server's image item carries it) of the declared ``media_type`` as an image to send,
    or why it is left out (a fixed phrase that completes "image omitted: ...")."""
    declared = media_type.strip().lower() if isinstance(media_type, str) else ""
    if declared not in IMAGE_TYPES:
        return "it is not a PNG or JPEG image"
    if not isinstance(data, str):
        return "its data is not text"
    if len(data) > SOURCE_MAX_BYTES * 3 // 2:  # base64, with room for the line breaks some encoders add
        return f"it is larger than {SOURCE_MAX_BYTES // 1_000_000} MB"
    try:
        raw = base64.b64decode("".join(data.split()), validate=True)  # line breaks are not part of the data
    except (binascii.Error, ValueError):
        return "its data is not valid base64"
    if len(raw) > SOURCE_MAX_BYTES:
        return f"it is larger than {SOURCE_MAX_BYTES // 1_000_000} MB"
    kind = IMAGE_TYPES[declared]
    if kind == "JPEG" and raw.count(_JPEG_SCAN) > JPEG_SCANS_MAX:
        return "it has too many scans"
    try:
        from PIL import Image
    except ImportError:
        return "no image library is installed here"
    try:
        with Image.open(io.BytesIO(raw), formats=(kind,)) as source:
            width, height = source.size
            if width < 1 or height < 1 or width * height > PIXELS_MAX:
                return "it has too many pixels"
            longer = max(width, height)
            if kind == "JPEG" and longer > LONG_EDGE:
                # Decoded at a half, a quarter or an eighth of its size when that is still at least what is sent.
                source.draft(None, (max(1, width * LONG_EDGE // longer), max(1, height * LONG_EDGE // longer)))
            source.load()  # the first frame, decoded whole: a damaged body fails here
            alpha = source.mode in ("RGBA", "LA", "PA") or "transparency" in source.info
            wanted = "RGBA" if alpha and declared == "image/png" else "RGB"
            if source.mode in ("I;16", "I;16L", "I;16B"):  # 16 bits a pixel: the high byte, not a clipped value
                high = slice(0, None, 2) if source.mode == "I;16B" else slice(1, None, 2)
                work = Image.frombytes("L", source.size, source.tobytes()[high])
            elif source.mode in ("RGB", "RGBA", "L") and "transparency" not in source.info:
                work = source  # scaled as it is: no second copy of a large image
            else:
                work = source.convert(wanted)  # a palette, a colour key, CMYK: scaled as the colours they stand for
            if max(work.size) > LONG_EDGE:
                work.thumbnail((LONG_EDGE, LONG_EDGE), Image.Resampling.LANCZOS)
            if work.mode != wanted:
                work = work.convert(wanted)
            # A new image from the pixels alone: what the source carried beside them stays behind with it.
            picture = Image.frombytes(work.mode, work.size, work.tobytes())
        out_type, encoded = declared, _encode(picture, kind)
        if out_type == "image/png" and len(encoded) > OUT_MAX_BYTES:
            out_type, encoded = "image/jpeg", _encode(_on_white(picture, Image), "JPEG")
        if len(encoded) > OUT_MAX_BYTES:
            return "it is too large to send"
        return OutsideImage(out_type, base64.b64encode(encoded).decode("ascii"), picture.width, picture.height)
    except Exception:  # Pillow raises many kinds (OSError, SyntaxError, ValueError, its own bomb error)
        return "it could not be read as an image"


def _encode(picture: Any, kind: str) -> bytes:
    out = io.BytesIO()
    if kind == "JPEG":
        picture.save(out, format="JPEG", quality=85)
    else:
        picture.save(out, format="PNG")
    return out.getvalue()


def _on_white(picture: Any, image_module: Any) -> Any:
    """``picture`` without its alpha channel, over white (a JPEG has none). A new image: nothing but pixels."""
    ground = image_module.new("RGB", picture.size, (255, 255, 255))
    if picture.mode == "RGBA":
        ground.paste(picture, mask=picture.getchannel("A"))
    else:
        ground.paste(picture.convert("RGB"))
    return ground
