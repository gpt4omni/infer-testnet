#!/usr/bin/env python3
"""Minimal PNG icon writer (stdlib only): teal rounded square, white diamond.
Used for the AppImage icon so packaging needs no Pillow dependency."""
import struct
import zlib


def png_chunk(typ: bytes, data: bytes) -> bytes:
    c = typ + data
    return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)


def main(path="infer-wallet.png", size=256):
    bg = (13, 110, 110)   # deep teal
    fg = (240, 240, 240)  # near-white diamond
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            dx, dy = abs(x - size / 2) / (size / 2), abs(y - size / 2) / (size / 2)
            edge = max(abs(x / size - 0.5), abs(y / size - 0.5)) * 2  # 0 center, 1 corner
            shade = max(0.35, 1.0 - edge * edge * 0.65)  # dark-teal corners, never black
            in_diamond = dx + dy < 0.62
            c = fg if in_diamond else tuple(int(v * shade) for v in bg)
            raw += bytes(c)
    png = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + png_chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + png_chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    print(f"wrote {path} ({len(png)} bytes)")


if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else "infer-wallet.png")
