"""Закрыть приложение по-человечески: WM_CLOSE, потом снять скрин окна подтверждения.

Печатает прямоугольник диалога и сохраняет его снимок, чтобы нажать кнопку по факту,
а не наугад.
"""
import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
TITLE = "Майнинг — панель"


def find(title: str) -> int:
    out = []

    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        if buf.value == title:
            out.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return out[0] if out else 0


def top_windows(pid: int) -> list[tuple[int, str, str, tuple]]:
    out = []

    def cb(hwnd, _):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value != pid or not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        ttl = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, ttl, 512)
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        out.append((hwnd, cls.value, ttl.value,
                    (rect.left, rect.top, rect.right, rect.bottom)))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return out


def shot(rect: tuple, path: str) -> None:
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    hdc = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    gdi32.BitBlt(mem, 0, 0, w, h, hdc, left, top, 0x00CC0020)
    bmpinfo = bytearray(40)
    bmpinfo[0:4] = (40).to_bytes(4, "little")
    bmpinfo[4:8] = w.to_bytes(4, "little", signed=True)
    bmpinfo[8:12] = (-h).to_bytes(4, "little", signed=True)
    bmpinfo[12:14] = (1).to_bytes(2, "little")
    bmpinfo[14:16] = (32).to_bytes(2, "little")
    size = w * h * 4
    buf = (ctypes.c_char * size)()
    gdi32.GetDIBits(mem, bmp, 0, h, buf, bytes(bmpinfo), 0)
    with open(path, "wb") as f:
        def chunk(tag: bytes, data: bytes) -> bytes:
            crc = 0xFFFFFFFF
            for byte in tag + data:
                crc ^= byte
                for _ in range(8):
                    crc = (crc >> 1) ^ (0xEDB88320 if crc & 1 else 0)
            return (len(data).to_bytes(4, "big") + tag + data
                    + (crc ^ 0xFFFFFFFF).to_bytes(4, "big"))
        raw = b"".join(b"\x00" + bytes(buf[y * w * 4:(y + 1) * w * 4]) for y in range(h))
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", w.to_bytes(4, "big") + h.to_bytes(4, "big") + bytes([8, 6, 0, 0, 0])))
        import zlib
        f.write(chunk(b"IDAT", zlib.compress(raw, 6)))
        f.write(chunk(b"IEND", b""))
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(0, hdc)


def main() -> None:
    hwnd = find(TITLE)
    if not hwnd:
        print("окно приложения не найдено")
        return
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    print(f"приложение hwnd={hwnd} pid={pid.value}", flush=True)
    user32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE
    for _ in range(30):
        time.sleep(0.2)
        wins = [w for w in top_windows(pid.value) if w[0] != hwnd]
        if wins:
            for w in wins:
                print(f"диалог hwnd={w[0]} {w[1]} «{w[2]}» {w[3]}", flush=True)
            shot(wins[0][3], str(Path(__file__).resolve().parent.parent
                                 / "notes" / "tmp-dialog.png"))
            print("снимок: notes/tmp-dialog.png", flush=True)
            return
    print("диалог не появился (приложение уже закрыто?)", flush=True)
    subprocess.run(["tasklist", "/FI", f"PID eq {pid.value}", "/NH"],
                   capture_output=True, text=True)


if __name__ == "__main__":
    main()
