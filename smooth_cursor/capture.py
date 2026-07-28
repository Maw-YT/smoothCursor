"""Capture live HCURSOR artwork (system + cross-process app cursors)."""

from __future__ import annotations

import ctypes
import hashlib
from dataclasses import dataclass

import win32api
import win32con
import win32gui
from PIL import Image

from .winapi import (
    BI_RGB,
    BITMAP,
    BITMAPINFO,
    BITMAPINFOHEADER,
    DIB_RGB_COLORS,
    ICONINFO,
    gdi32,
    user32,
)

# Guard against corrupt / absurd cursor bitmaps (was crashing Python on some Chrome cursors).
_MAX_CURSOR_SIDE = 256
_MAX_CURSOR_PIXELS = _MAX_CURSOR_SIDE * _MAX_CURSOR_SIDE


@dataclass
class CursorFrame:
    image: Image.Image
    hotspot: tuple[int, int]
    fingerprint: bytes = b""


def _fingerprint(img: Image.Image) -> bytes:
    rgba = img.convert("RGBA")
    header = f"{rgba.size[0]}x{rgba.size[1]}|".encode()
    return hashlib.blake2b(header + rgba.tobytes(), digest_size=16).digest()


def fingerprints_similar(a: bytes | None, b: bytes | None, max_bad_pixels: int = 1) -> bool:
    if a is None or b is None:
        return False
    return a == b


def _dims_ok(w: int, h: int) -> bool:
    return 1 <= w <= _MAX_CURSOR_SIDE and 1 <= h <= _MAX_CURSOR_SIDE and w * h <= _MAX_CURSOR_PIXELS


def frame_is_opaque_black_fill(frame: CursorFrame | None) -> bool:
    """
    True when the bitmap is (near-)solid opaque black — every sample is
    rgb≈0 with alpha≈255. Those are bad captures / broken encodes; leave the
    native cursor alone (same idea as not tilting .ani spinners).
    """
    if frame is None:
        return False
    img = frame.image
    if not _dims_ok(img.width, img.height):
        return False
    px = img.load()
    total = 0
    solid = 0
    step = 1 if max(img.width, img.height) <= 64 else 2
    for y in range(0, img.height, step):
        for x in range(0, img.width, step):
            r, g, b, a = px[x, y]
            total += 1
            if a >= 250 and r <= 8 and g <= 8 and b <= 8:
                solid += 1
    if total < 4:
        return False
    # ≥92% of the bitmap is fully opaque black → ignore.
    return solid >= int(total * 0.92)


def _rgba_is_black_alpha_ink(img: Image.Image) -> bool:
    """
    Black (+ soft grey AA) ink on transparency — Chrome I-beam / resize / etc.

    Allows grey antialiasing, but rejects white-outline scheme art and any
    chromatic (colored) cursors.
    """
    if not _dims_ok(img.width, img.height):
        return False
    px = img.load()
    dark = 0
    bright = 0
    chromatic = 0
    step = 1 if max(img.width, img.height) <= 64 else 2
    for y in range(0, img.height, step):
        for x in range(0, img.width, step):
            r, g, b, a = px[x, y]
            if a < 16:
                continue
            if max(r, g, b) - min(r, g, b) > 28:
                chromatic += 1
                continue
            lum = (r + g + b) / 3.0
            if lum > 140:
                bright += 1
            else:
                # Near-black and grey AA both count as ink.
                dark += 1
    if chromatic > 2:
        return False
    if dark < 3:
        return False
    # White-outline pointers (scheme Text/Normal) have lots of bright pixels.
    if bright > max(2, dark // 3):
        return False
    return True


def frame_is_black_alpha(frame: CursorFrame | None) -> bool:
    """
    Pure black ink + alpha (I-beam / resize / solid black fills).

    Rebuilding these every frame is expensive and often looks wrong — passthrough
    the native HCURSOR instead (same treatment as .ani: don't pose/rebuild).
    """
    if frame is None:
        return False
    if frame_is_opaque_black_fill(frame):
        return True
    return _rgba_is_black_alpha_ink(frame.image.convert("RGBA"))


def probe_cursor_passthrough(hicon: int) -> bool:
    """
    Cheap preflight — True if we should leave this HCURSOR alone.

    Avoids the full icon_to_cursor_frame pipeline (especially dual-background),
    which is what lagged on Chrome black+alpha cursors.
    """
    if not hicon:
        return False
    try:
        hicon = int(hicon)
    except Exception:
        return False

    info = ICONINFO()
    if not user32.GetIconInfo(hicon, ctypes.byref(info)):
        return False
    try:
        # Classic mono AND/XOR cursor (no color plane) → always passthrough.
        if not info.hbmColor:
            return True

        bmp = BITMAP()
        if not gdi32.GetObjectW(info.hbmColor, ctypes.sizeof(bmp), ctypes.byref(bmp)):
            return False
        w = int(bmp.bmWidth)
        h = int(abs(bmp.bmHeight))
        if not _dims_ok(w, h):
            return True

        # Read the color DIB directly (no dual-background dance).
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        buf = (ctypes.c_ubyte * (w * h * 4))()
        hdc = win32gui.GetDC(0)
        try:
            got = gdi32.GetDIBits(
                hdc, info.hbmColor, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS
            )
        finally:
            win32gui.ReleaseDC(0, hdc)
        if not got:
            return False

        # Mask-style black glyphs (Chrome I-beam / resize): color DIB is all
        # zeros and the shape lives in the AND mask. Never try to rebuild those.
        nonzero = 0
        for i in range(0, w * h * 4, 4):
            if buf[i] | buf[i + 1] | buf[i + 2] | buf[i + 3]:
                nonzero += 1
                if nonzero >= 3:
                    break
        if nonzero < 3:
            return True

        img = Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "BGRA", 0, 1).copy()
        px = img.load()
        for y in range(h):
            for x in range(w):
                r, g, b, a = px[x, y]
                if a < 16:
                    px[x, y] = (0, 0, 0, 0)
                elif a < 255 and a > 0:
                    px[x, y] = (
                        min(255, r * 255 // a),
                        min(255, g * 255 // a),
                        min(255, b * 255 // a),
                        a,
                    )

        if frame_is_opaque_black_fill(CursorFrame(img, (0, 0), b"")):
            return True
        return _rgba_is_black_alpha_ink(img)
    except Exception:
        return False
    finally:
        if info.hbmColor:
            gdi32.DeleteObject(info.hbmColor)
        if info.hbmMask:
            gdi32.DeleteObject(info.hbmMask)


def frame_is_usable(frame: CursorFrame) -> bool:
    """Accept thin black glyphs (I-beam / resize); reject empty or solid-fill junk."""
    if frame_is_opaque_black_fill(frame):
        return False
    img = frame.image
    if not _dims_ok(img.width, img.height):
        return False
    px = img.load()
    opaque = 0
    black = 0
    colored = 0
    min_x, min_y = img.width, img.height
    max_x, max_y = -1, -1
    step = 1
    for y in range(0, img.height, step):
        for x in range(0, img.width, step):
            r, g, b, a = px[x, y]
            if a < 16:
                continue
            opaque += 1
            if x < min_x:
                min_x = x
            if y < min_y:
                min_y = y
            if x > max_x:
                max_x = x
            if y > max_y:
                max_y = y
            if r < 28 and g < 28 and b < 28:
                black += 1
            elif r > 40 or g > 40 or b > 40:
                colored += 1
    if opaque < 3:
        return False
    # Solid black rectangle fill (bad capture / broken encode) — reject.
    # Thin beams fill their tight bbox completely; only reject chunky blobs.
    if max_x >= min_x and colored < 2 and black >= max(3, int(opaque * 0.85)):
        bw = max_x - min_x + 1
        bh = max_y - min_y + 1
        bbox_area = bw * bh
        fill = opaque / max(1, bbox_area)
        if bw >= 12 and bh >= 12 and fill >= 0.55:
            return False
    return True


def frame_is_blank(frame: CursorFrame | None) -> bool:
    if frame is None:
        return True
    img = frame.image
    px = img.load()
    for y in range(0, img.height, 2):
        for x in range(0, img.width, 2):
            if px[x, y][3] >= 8:
                return False
    return True


def _icon_size_and_hotspot(hicon: int) -> tuple[int, int, int, int]:
    width = height = win32api.GetSystemMetrics(win32con.SM_CXCURSOR)
    hx = hy = width // 2
    info = ICONINFO()
    if user32.GetIconInfo(hicon, ctypes.byref(info)):
        try:
            hx, hy = int(info.xHotspot), int(info.yHotspot)
            bmp = BITMAP()
            hb = info.hbmColor or info.hbmMask
            if hb and gdi32.GetObjectW(hb, ctypes.sizeof(bmp), ctypes.byref(bmp)):
                width = int(bmp.bmWidth)
                height = int(abs(bmp.bmHeight))
                if info.hbmColor is None and info.hbmMask:
                    height = max(1, height // 2)
        finally:
            if info.hbmColor:
                gdi32.DeleteObject(info.hbmColor)
            if info.hbmMask:
                gdi32.DeleteObject(info.hbmMask)
    width = max(1, min(_MAX_CURSOR_SIDE, width))
    height = max(1, min(_MAX_CURSOR_SIDE, height))
    return width, height, hx, hy


def _from_color_bitmap(hicon: int) -> CursorFrame | None:
    info = ICONINFO()
    if not user32.GetIconInfo(hicon, ctypes.byref(info)):
        return None
    try:
        if not info.hbmColor:
            return None
        bmp = BITMAP()
        if not gdi32.GetObjectW(info.hbmColor, ctypes.sizeof(bmp), ctypes.byref(bmp)):
            return None
        w, h = int(bmp.bmWidth), int(abs(bmp.bmHeight))
        if not _dims_ok(w, h):
            return None
        hx, hy = int(info.xHotspot), int(info.yHotspot)

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        buf = (ctypes.c_ubyte * (w * h * 4))()
        hdc = win32gui.GetDC(0)
        try:
            got = gdi32.GetDIBits(
                hdc, info.hbmColor, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS
            )
        finally:
            win32gui.ReleaseDC(0, hdc)
        if not got:
            return None

        img = Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "BGRA", 0, 1).copy()
        px = img.load()
        soft = 0
        premult_votes = 0
        max_a = 0
        for y in range(0, h, 2):
            for x in range(0, w, 2):
                r, g, b, a = px[x, y]
                if a > max_a:
                    max_a = a
                if 0 < a < 255:
                    soft += 1
                    if r <= a and g <= a and b <= a and max(r, g, b) < a - 8:
                        premult_votes += 1
        if soft > 0 and premult_votes * 2 >= soft:
            for y in range(h):
                for x in range(w):
                    r, g, b, a = px[x, y]
                    if 0 < a < 255:
                        px[x, y] = (
                            min(255, r * 255 // a),
                            min(255, g * 255 // a),
                            min(255, b * 255 // a),
                            a,
                        )

        # Classic masked color cursors store alpha=0 everywhere; rebuild alpha
        # from the AND mask so we can re-capture our own encodes.
        if max_a < 8 and info.hbmMask:
            mbmp = BITMAP()
            if gdi32.GetObjectW(info.hbmMask, ctypes.sizeof(mbmp), ctypes.byref(mbmp)):
                mw, mh = int(mbmp.bmWidth), int(abs(mbmp.bmHeight))
                if mw == w and mh >= h:
                    stride = ((w + 15) // 16) * 2
                    mask_buf = (ctypes.c_ubyte * (stride * h))()
                    # Read top h scanlines of the mask as top-down via GetBitmapBits
                    got_m = gdi32.GetBitmapBits(info.hbmMask, stride * h, mask_buf)
                    if got_m:
                        for y in range(h):
                            for x in range(w):
                                bit = (mask_buf[y * stride + (x // 8)] >> (7 - (x % 8))) & 1
                                r, g, b, _ = px[x, y]
                                if bit:
                                    px[x, y] = (0, 0, 0, 0)
                                else:
                                    px[x, y] = (r, g, b, 255)

        frame = CursorFrame(img, (hx, hy), _fingerprint(img))
        if frame_is_usable(frame):
            return frame
        return None
    finally:
        if info.hbmColor:
            gdi32.DeleteObject(info.hbmColor)
        if info.hbmMask:
            gdi32.DeleteObject(info.hbmMask)


def _from_monochrome_mask(hicon: int) -> CursorFrame | None:
    """Decode AND/XOR mask cursors (common for I-beam / resize / vertical text)."""
    info = ICONINFO()
    if not user32.GetIconInfo(hicon, ctypes.byref(info)):
        return None
    try:
        if info.hbmColor or not info.hbmMask:
            return None
        bmp = BITMAP()
        if not gdi32.GetObjectW(info.hbmMask, ctypes.sizeof(bmp), ctypes.byref(bmp)):
            return None
        w = int(bmp.bmWidth)
        full_h = int(abs(bmp.bmHeight))
        if w <= 0 or full_h < 2:
            return None
        h = full_h // 2
        if not _dims_ok(w, h):
            return None
        hx, hy = int(info.xHotspot), int(info.yHotspot)

        # Draw on black + white; reconstruct opaque black/white glyph with transparency.
        black = _draw_icon_on_bg(hicon, w, h, (0, 0, 0))
        white = _draw_icon_on_bg(hicon, w, h, (255, 255, 255))
        if not black or not white:
            return None
        out = bytearray(w * h * 4)
        for i in range(0, w * h * 4, 4):
            bb, bg, br = black[i], black[i + 1], black[i + 2]
            wb, wg, wr = white[i], white[i + 1], white[i + 2]
            same = abs(bb - wb) + abs(bg - wg) + abs(br - wr) < 12
            if same:
                # Unchanged vs both backgrounds → transparent (or pure XOR invert).
                if bb > 200 and wb < 40:
                    out[i : i + 4] = b"\x00\x00\x00\xff"  # inverted → show black
                else:
                    out[i : i + 4] = b"\x00\x00\x00\x00"
                continue
            # Covered pixel: use black-bg color as the glyph.
            if bb < 48 and bg < 48 and br < 48:
                out[i : i + 4] = b"\x00\x00\x00\xff"
            elif bb > 200 and bg > 200 and br > 200:
                out[i : i + 4] = b"\xff\xff\xff\xff"
            else:
                out[i] = bb
                out[i + 1] = bg
                out[i + 2] = br
                out[i + 3] = 255
        img = Image.frombuffer("RGBA", (w, h), bytes(out), "raw", "BGRA", 0, 1).copy()
        frame = CursorFrame(img, (hx, hy), _fingerprint(img))
        if frame_is_usable(frame):
            return frame
        return None
    finally:
        if info.hbmColor:
            gdi32.DeleteObject(info.hbmColor)
        if info.hbmMask:
            gdi32.DeleteObject(info.hbmMask)


def _draw_icon_on_bg(hicon: int, w: int, h: int, bg_bgr: tuple[int, int, int]) -> bytes | None:
    if not _dims_ok(w, h):
        return None
    hdc_screen = win32gui.GetDC(0)
    try:
        hdc_mem = win32gui.CreateCompatibleDC(hdc_screen)
        try:
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = BI_RGB
            bits_ptr = ctypes.c_void_p()
            hbmp = gdi32.CreateDIBSection(
                hdc_mem, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits_ptr), None, 0
            )
            if not hbmp or not bits_ptr:
                return None
            kb, kg, kr = bg_bgr
            ctypes.memmove(bits_ptr, bytes([kb, kg, kr, 255]) * (w * h), w * h * 4)
            old = win32gui.SelectObject(hdc_mem, hbmp)
            user32.DrawIconEx(hdc_mem, 0, 0, hicon, w, h, 0, None, win32con.DI_NORMAL)
            raw = ctypes.string_at(bits_ptr, w * h * 4)
            win32gui.SelectObject(hdc_mem, old)
            win32gui.DeleteObject(hbmp)
            return raw
        finally:
            win32gui.DeleteDC(hdc_mem)
    finally:
        win32gui.ReleaseDC(0, hdc_screen)


def _from_dual_background(hicon: int) -> CursorFrame | None:
    width, height, hx, hy = _icon_size_and_hotspot(hicon)
    sizes = [(width, height), (32, 32), (48, 48), (64, 64), (128, 128)]
    tried: set[tuple[int, int]] = set()
    for w, h in sizes:
        if not _dims_ok(w, h) or (w, h) in tried:
            continue
        tried.add((w, h))
        black = _draw_icon_on_bg(hicon, w, h, (0, 0, 0))
        white = _draw_icon_on_bg(hicon, w, h, (255, 255, 255))
        if not black or not white:
            continue
        out = bytearray(w * h * 4)
        for i in range(0, w * h * 4, 4):
            bb, bg, br = black[i], black[i + 1], black[i + 2]
            wb, wg, wr = white[i], white[i + 1], white[i + 2]
            diff = (abs(wb - bb) + abs(wg - bg) + abs(wr - br)) / 3.0
            a = int(max(0, min(255, round(255 - diff))))
            if a < 2:
                out[i : i + 4] = b"\x00\x00\x00\x00"
                continue
            out[i] = min(255, bb * 255 // a)
            out[i + 1] = min(255, bg * 255 // a)
            out[i + 2] = min(255, br * 255 // a)
            out[i + 3] = a
        img = Image.frombuffer("RGBA", (w, h), bytes(out), "raw", "BGRA", 0, 1).copy()
        frame = CursorFrame(img, (hx, hy), _fingerprint(img))
        if frame_is_usable(frame):
            return frame
    return None


def _from_drawiconex_alpha(hicon: int) -> CursorFrame | None:
    width, height, hx, hy = _icon_size_and_hotspot(hicon)
    sizes = [(width, height), (32, 32), (48, 48), (64, 64)]
    tried: set[tuple[int, int]] = set()
    hdc_screen = win32gui.GetDC(0)
    try:
        for w, h in sizes:
            if not _dims_ok(w, h) or (w, h) in tried:
                continue
            tried.add((w, h))
            hdc_mem = win32gui.CreateCompatibleDC(hdc_screen)
            try:
                bmi = BITMAPINFO()
                bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                bmi.bmiHeader.biWidth = w
                bmi.bmiHeader.biHeight = -h
                bmi.bmiHeader.biPlanes = 1
                bmi.bmiHeader.biBitCount = 32
                bmi.bmiHeader.biCompression = BI_RGB
                bits_ptr = ctypes.c_void_p()
                hbmp = gdi32.CreateDIBSection(
                    hdc_mem, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits_ptr), None, 0
                )
                if not hbmp or not bits_ptr:
                    continue
                old = win32gui.SelectObject(hdc_mem, hbmp)
                ctypes.memset(bits_ptr, 0, w * h * 4)
                user32.DrawIconEx(hdc_mem, 0, 0, hicon, w, h, 0, None, win32con.DI_NORMAL)
                raw = ctypes.string_at(bits_ptr, w * h * 4)
                img = Image.frombuffer("RGBA", (w, h), raw, "raw", "BGRA", 0, 1).copy()
                px = img.load()
                for y in range(h):
                    for x in range(w):
                        r, g, b, a = px[x, y]
                        if 0 < a < 255:
                            px[x, y] = (
                                min(255, r * 255 // a),
                                min(255, g * 255 // a),
                                min(255, b * 255 // a),
                                a,
                            )
                win32gui.SelectObject(hdc_mem, old)
                win32gui.DeleteObject(hbmp)
                frame = CursorFrame(img, (hx, hy), _fingerprint(img))
                if frame_is_usable(frame):
                    return frame
            finally:
                win32gui.DeleteDC(hdc_mem)
    finally:
        win32gui.ReleaseDC(0, hdc_screen)
    return None


def icon_to_cursor_frame(hicon: int) -> CursorFrame | None:
    if not hicon:
        return None
    try:
        hicon = int(hicon)
    except Exception:
        return None
    copied = 0
    try:
        copied = int(user32.CopyIcon(hicon) or 0)
    except Exception:
        copied = 0
    targets = []
    if copied:
        targets.append(copied)
    targets.append(hicon)
    try:
        for target in targets:
            # Mono first — Chrome vertical-text / resize are often AND/XOR masks.
            # Dual-background is last (expensive; was lagging the main loop).
            for method in (
                _from_color_bitmap,
                _from_monochrome_mask,
                _from_drawiconex_alpha,
                # _from_dual_background intentionally omitted — it was a major
                # source of lag on Chrome black+alpha cursors; those are now
                # handled by probe_cursor_passthrough instead.
            ):
                try:
                    frame = method(target)
                except Exception:
                    frame = None
                if frame is not None and frame_is_usable(frame) and not frame_is_blank(frame):
                    return frame
        return None
    finally:
        if copied:
            try:
                user32.DestroyIcon(copied)
            except Exception:
                pass
