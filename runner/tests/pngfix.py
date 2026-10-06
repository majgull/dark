"""Synthetic PNGs for tests, built in memory.

The tree keeps no binary fixture: `runner/ops/no-binaries.sh`, which the gate
runs, refuses a tracked file that is not text. A test that needs a screenshot
therefore builds one here.

`png(width, height, pixel)` is an 8-bit, non-interlaced truecolour PNG with
filter 0 on every row. `pixel` is one `(r, g, b)` for a frame that is a single
colour, or a callable `f(x, y) -> (r, g, b)` for one that varies.
"""

import struct
import zlib


def png(width, height, pixel=(0, 0, 0)):
    """The bytes of a `width` x `height` RGB PNG drawn from `pixel`."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type None
        for x in range(width):
            c = pixel(x, y) if callable(pixel) else pixel
            raw += bytes(c)

    def chunk(typ, data):
        body = typ + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))
