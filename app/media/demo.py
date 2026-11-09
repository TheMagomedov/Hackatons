"""Explicit demo provider; never selected as a fallback."""

import binascii
import struct
import zlib

from app.shared.models import BusinessProfile, GeneratedImage, TextDraft

from .limits import MediaLimits
from .validation import DEFAULT_LIMITS, PNG_SIGNATURE, build_image_prompt, validate_limits, validate_png


def _chunk(name: bytes, data: bytes) -> bytes:
    crc = binascii.crc32(name + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + name + data + struct.pack(">I", crc)


# A neutral, valid 1 x 1 RGB PNG. This placeholder is not a generated creative.
_DEMO_PNG = (
    PNG_SIGNATURE
    + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    + _chunk(b"IDAT", zlib.compress(b"\x00\xc8\xc8\xc8"))
    + _chunk(b"IEND", b"")
)


class DemoImageProvider:
    is_demo = True

    def __init__(self, *, limits: MediaLimits = DEFAULT_LIMITS):
        validate_limits(limits)
        self._limits = limits

    async def generate(
        self, profile: BusinessProfile, text: TextDraft, feedback: str | None = None
    ) -> GeneratedImage:
        build_image_prompt(profile, text, feedback, self._limits)
        validate_png(_DEMO_PNG, self._limits)
        return GeneratedImage(
            content=_DEMO_PNG,
            mime_type="image/png",
            provider="demo",
            is_demo=True,
        )
