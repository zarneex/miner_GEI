"""Снимок участка экрана (вместе с всплывающими подсказками) в PNG.

Подсказки Qt — отдельные окна, PrintWindow главного окна их не видит, поэтому
для проверки подсказок снимаем экран через BitBlt: x y w h путь.
"""
import ctypes
import ctypes.wintypes as wt
import sys
import zlib

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


def png_chunk(tag: bytes, data: bytes) -> bytes:
    crc = 0xFFFFFFFF
    for byte in tag + data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 if crc & 1 else 0)
    return (len(data).to_bytes(4, "big") + tag + data
            + (crc ^ 0xFFFFFFFF).to_bytes(4, "big"))


def shot(x: int, y: int, w: int, h: int, path: str) -> None:
    hdc = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    gdi32.BitBlt(mem, 0, 0, w, h, hdc, x, y, 0x00CC0020)
    info = bytearray(40)
    info[0:4] = (40).to_bytes(4, "little")
    info[4:8] = w.to_bytes(4, "little", signed=True)
    info[8:12] = (-h).to_bytes(4, "little", signed=True)
    info[12:14] = (1).to_bytes(2, "little")
    info[14:16] = (32).to_bytes(2, "little")
    buf = (ctypes.c_char * (w * h * 4))()
    gdi32.GetDIBits(mem, bmp, 0, h, buf, bytes(info), 0)
    raw = b"".join(b"\x00" + bytes(buf[row * w * 4:(row + 1) * w * 4]) for row in range(h))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(png_chunk(b"IHDR", w.to_bytes(4, "big") + h.to_bytes(4, "big")
                          + bytes([8, 6, 0, 0, 0])))
        f.write(png_chunk(b"IDAT", zlib.compress(raw, 6)))
        f.write(png_chunk(b"IEND", b""))
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(0, hdc)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    shot(int(a[0]), int(a[1]), int(a[2]), int(a[3]), a[4])
    print(f"saved: {a[4]}", flush=True)
