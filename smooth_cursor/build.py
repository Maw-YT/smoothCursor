"""Build rotated HCURSORs from PIL images."""

from __future__ import annotations

import ctypes
import math
from functools import lru_cache

import win32gui
from PIL import Image, ImageDraw, ImageFont

from .winapi import (
    BI_BITFIELDS,
    BI_RGB,
    BITMAPINFOHEADER,
    BITMAPV5HEADER,
    DIB_RGB_COLORS,
    ICONINFO,
    gdi32,
    user32,
)

# Hardware cursors past this get flaky on many GPUs; rotation canvas is cropped down.
_MAX_CURSOR_SIDE = 128

BADGE_THEMES: dict[str, tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]] = {
    # name → (fill, text, outline)
    "Teal": ((45, 212, 191), (15, 17, 21), (15, 17, 21)),
    "Coral": ((251, 113, 133), (15, 17, 21), (15, 17, 21)),
    "Amber": ((251, 191, 36), (15, 17, 21), (15, 17, 21)),
    "Sky": ((56, 189, 248), (15, 17, 21), (15, 17, 21)),
    "Ink": ((30, 35, 45), (232, 236, 244), (45, 212, 191)),
    "Ghost": ((232, 236, 244), (15, 17, 21), (42, 49, 64)),
}

BADGE_STYLES = ("Dot", "Pill", "Square", "Soft")

# Display name → Windows font file candidates (first that loads wins).
BADGE_FONTS: dict[str, tuple[str, ...]] = {
    "Segoe UI": ("segoeui.ttf", "seguisb.ttf"),
    "Segoe UI Bold": ("seguisb.ttf", "segoeui.ttf"),
    "Consolas": ("consola.ttf", "consolab.ttf"),
    "Cascadia Mono": ("CascadiaMono.ttf", "CascadiaCode.ttf"),
    "Calibri": ("calibri.ttf", "calibril.ttf"),
    "Georgia": ("georgia.ttf", "georgiab.ttf"),
    "Verdana": ("verdana.ttf", "verdanab.ttf"),
    "Trebuchet MS": ("trebuc.ttf", "trebucbd.ttf"),
    "Comic Sans": ("comic.ttf", "comicbd.ttf"),
    "Arial": ("arial.ttf", "arialbd.ttf"),
}


@lru_cache(maxsize=32)
def _badge_font(
    family: str, size: int
) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    candidates = BADGE_FONTS.get(family) or BADGE_FONTS["Segoe UI"]
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    for name in ("segoeui.ttf", "arial.ttf", "calibri.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_key_badge(
    img: Image.Image,
    hx: int,
    hy: int,
    label: str,
    alpha: float,
    *,
    style: str = "Pill",
    theme: str = "Teal",
    size: float = 1.0,
    font_family: str = "Segoe UI",
    count: int = 1,
) -> tuple[Image.Image, int, int]:
    """
    Paste a notification-style key / shortcut badge near the cursor tip.
    Returns (image, hotspot_x, hotspot_y) — canvas grows so text never crops.
    """
    if not label or alpha <= 0.02:
        return img, hx, hy

    img = img.convert("RGBA")
    a = max(0.0, min(1.0, float(alpha)))
    size = max(0.7, min(1.6, float(size)))
    style_key = style if style in BADGE_STYLES else "Pill"
    fill_rgb, text_rgb, outline_rgb = BADGE_THEMES.get(theme, BADGE_THEMES["Teal"])
    family = font_family if font_family in BADGE_FONTS else "Segoe UI"

    text = label.strip()
    if not text:
        return img, hx, hy
    count = max(1, int(count))
    if count > 1:
        text = f"{text} ×{min(count, 999)}"

    is_combo = "+" in text or "×" in text or len(text) > 2
    font_size = max(8, int(round((10 if is_combo else 12) * size)))
    font = _badge_font(family, font_size)

    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    try:
        bbox = probe.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        t_off_x, t_off_y = -bbox[0], -bbox[1]
    except Exception:
        tw, th = max(1, len(text) * font_size // 2), font_size
        t_off_x, t_off_y = 0, 0

    pad_x = max(5, int(round(6 * size)))
    pad_y = max(3, int(round(4 * size)))

    # Always size the badge to the full text — never clip glyphs.
    if style_key == "Dot" and not is_combo and count <= 1 and len(text) <= 2:
        d = max(tw + pad_x * 2, th + pad_y * 2, int(round(16 * size)))
        bw = bh = d
        radius = d // 2
    elif style_key == "Square":
        side = max(14, int(round(16 * size)))
        bw = max(side, tw + pad_x * 2)
        bh = max(side, th + pad_y * 2)
        radius = max(2, int(round(3 * size)))
    elif style_key == "Soft":
        bw = tw + pad_x * 2 + 4
        bh = max(int(round(16 * size)), th + pad_y * 2)
        radius = bh // 2
    else:  # Pill (default for long / combo / counted labels)
        bw = tw + pad_x * 2 + 4
        bh = max(int(round(16 * size)), th + pad_y * 2)
        radius = bh // 2

    # Build badge at 2× then downscale for a smooth antialiased outline.
    ss = 2
    badge_hi = Image.new("RGBA", (bw * ss, bh * ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(badge_hi)
    fill = (*fill_rgb, int(230 * a))
    outline = (*outline_rgb, int(255 * a))
    text_fill = (*text_rgb, int(255 * a))
    r_hi = max(1, radius * ss)
    box = (0, 0, bw * ss - 1, bh * ss - 1)
    if style_key == "Dot" and not is_combo and count <= 1 and len(label.strip()) <= 2:
        draw.ellipse(box, fill=fill, outline=outline, width=ss)
    else:
        draw.rounded_rectangle(box, radius=r_hi, fill=fill, outline=outline, width=ss)

    font_hi = _badge_font(family, font_size * ss)
    try:
        bbox_hi = ImageDraw.Draw(Image.new("RGBA", (8, 8))).textbbox(
            (0, 0), text, font=font_hi
        )
        tw_hi = bbox_hi[2] - bbox_hi[0]
        th_hi = bbox_hi[3] - bbox_hi[1]
        t_off_x_hi, t_off_y_hi = -bbox_hi[0], -bbox_hi[1]
    except Exception:
        tw_hi, th_hi = tw * ss, th * ss
        t_off_x_hi, t_off_y_hi = 0, 0
    tx = (bw * ss - tw_hi) / 2 + t_off_x_hi
    ty = (bh * ss - th_hi) / 2 + t_off_y_hi
    draw.text((tx, ty), text, font=font_hi, fill=text_fill)

    badge = badge_hi.resize((bw, bh), Image.Resampling.LANCZOS)

    # Canvas margins sized so the badge never crops off any edge.
    gap = 4
    margin_l = gap
    margin_t = bh + gap + 2
    margin_r = bw + gap + 8
    margin_b = gap
    canvas = Image.new(
        "RGBA",
        (img.width + margin_l + margin_r, img.height + margin_t + margin_b),
        (0, 0, 0, 0),
    )
    ox, oy = margin_l, margin_t
    canvas.paste(img, (ox, oy), img)
    hx2, hy2 = hx + ox, hy + oy

    bx = hx2 + max(4, img.width // 5)
    by = hy2 - bh - 2
    # Clamp inside canvas (margins guarantee room).
    bx = max(0, min(canvas.width - bw, bx))
    by = max(0, min(canvas.height - bh, by))

    canvas.alpha_composite(badge, (int(bx), int(by)))
    return canvas, hx2, hy2


def rotate_around_hotspot(
    img: Image.Image,
    hx: int,
    hy: int,
    angle: float,
    scale: float = 1.0,
) -> tuple[Image.Image, int, int]:
    """
    Scale uniformly, rotate around hotspot, then crop to content so the HCURSOR stays small.
    """
    img = img.convert("RGBA")
    sx = max(0.35, min(1.85, float(scale)))
    if abs(sx - 1.0) > 0.001:
        nw = max(1, int(round(img.width * sx)))
        nh = max(1, int(round(img.height * sx)))
        img = img.resize((nw, nh), Image.BICUBIC)
        hx = int(round(hx * sx))
        hy = int(round(hy * sx))

    hx = max(0, min(img.width, int(hx)))
    hy = max(0, min(img.height, int(hy)))

    if abs(angle) < 0.01 and abs(sx - 1.0) < 0.001:
        return _crop_to_content(img, hx, hy)

    ow, oh = img.size
    corners = (
        (0 - hx, 0 - hy),
        (ow - hx, 0 - hy),
        (0 - hx, oh - hy),
        (ow - hx, oh - hy),
    )
    radius = max(math.hypot(dx, dy) for dx, dy in corners)
    side = int(math.ceil(radius * 2)) + 16
    side = max(side, ow + 16, oh + 16)
    side = min(side, 512)

    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    ox = int(round(side / 2 - hx))
    oy = int(round(side / 2 - hy))
    canvas.paste(img, (ox, oy), img)
    rotated = canvas.rotate(
        angle,
        resample=Image.BICUBIC,
        expand=False,
        center=(side / 2, side / 2),
        fillcolor=(0, 0, 0, 0),
    )
    return _crop_to_content(rotated, side // 2, side // 2)


def _crop_to_content(
    img: Image.Image, hx: int, hy: int, pad: int = 3
) -> tuple[Image.Image, int, int]:
    alpha = img.getchannel("A")
    bbox = alpha.getbbox()
    if bbox is None:
        return img, hx, hy
    left, top, right, bottom = bbox
    left = max(0, left - pad)
    top = max(0, top - pad)
    right = min(img.width, right + pad)
    bottom = min(img.height, bottom + pad)
    cropped = img.crop((left, top, right, bottom))
    hx = hx - left
    hy = hy - top
    w, h = cropped.size
    if max(w, h) > _MAX_CURSOR_SIDE:
        s = _MAX_CURSOR_SIDE / max(w, h)
        nw = max(1, int(round(w * s)))
        nh = max(1, int(round(h * s)))
        cropped = cropped.resize((nw, nh), Image.BICUBIC)
        hx = int(round(hx * s))
        hy = int(round(hy * s))
    hx = max(0, min(cropped.width - 1, hx))
    hy = max(0, min(cropped.height - 1, hy))
    return cropped, hx, hy


def frame_is_pure_dark(img: Image.Image) -> bool:
    """Near-black ink only (Chrome vertical-text / resize). Kept for callers."""
    img = img.convert("RGBA")
    px = img.load()
    dark = 0
    lit = 0
    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = px[x, y]
            if a < 16:
                continue
            if max(r, g, b) >= 40:
                lit += 1
            else:
                dark += 1
    return dark >= 3 and lit <= max(2, dark // 30)


def _has_soft_alpha(img: Image.Image) -> bool:
    px = img.load()
    step = 1 if max(img.size) <= 64 else 2
    for y in range(0, img.height, step):
        for x in range(0, img.width, step):
            a = px[x, y][3]
            if 24 < a < 230:
                return True
    return False


def _as_masked_color_hcursor(img: Image.Image, hx: int, hy: int) -> int | None:
    """
    Classic color cursor: transparency from the AND mask, not alpha.

    Critical: every alpha byte in the color DIB must be 0. If any alpha is
    nonzero, Windows switches to alpha mode; pure-black glyphs then paint
    transparent (0,0,0,0) as opaque black → solid black square on the
    *hardware* cursor (DrawIconEx can still look fine).
    """
    img = img.convert("RGBA")
    w, h = img.size
    if w <= 0 or h <= 0 or w > 512 or h > 512:
        return None
    px = img.load()

    bi = BITMAPINFOHEADER()
    ctypes.memset(ctypes.byref(bi), 0, ctypes.sizeof(bi))
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth = w
    bi.biHeight = -h  # top-down; matches CreateBitmap mask orientation
    bi.biPlanes = 1
    bi.biBitCount = 32
    bi.biCompression = BI_RGB

    hdc = win32gui.GetDC(0)
    try:
        bits_ptr = ctypes.c_void_p()
        hbm_color = gdi32.CreateDIBSection(
            hdc, ctypes.byref(bi), DIB_RGB_COLORS, ctypes.byref(bits_ptr), None, 0
        )
        if not hbm_color or not bits_ptr:
            return None
        buf = (ctypes.c_ubyte * (w * h * 4)).from_address(bits_ptr.value)
        stride = ((w + 15) // 16) * 2
        mask = bytearray(stride * h)
        for y in range(h):
            for x in range(w):
                r, g, b, a = px[x, y]
                i = (y * w + x) * 4
                if a < 128:
                    buf[i] = buf[i + 1] = buf[i + 2] = 0
                    buf[i + 3] = 0
                    mask[y * stride + (x // 8)] |= 1 << (7 - (x % 8))
                else:
                    # Force alpha byte 0 so Windows uses the AND mask path.
                    buf[i] = b
                    buf[i + 1] = g
                    buf[i + 2] = r
                    buf[i + 3] = 0
    finally:
        win32gui.ReleaseDC(0, hdc)

    hbm_mask = gdi32.CreateBitmap(
        w, h, 1, 1, (ctypes.c_ubyte * len(mask)).from_buffer_copy(mask)
    )
    if not hbm_mask:
        gdi32.DeleteObject(hbm_color)
        return None
    try:
        ii = ICONINFO()
        ii.fIcon = False
        ii.xHotspot = hx
        ii.yHotspot = hy
        ii.hbmMask = hbm_mask
        ii.hbmColor = hbm_color
        hcursor = user32.CreateIconIndirect(ctypes.byref(ii))
        return int(hcursor) if hcursor else None
    finally:
        gdi32.DeleteObject(hbm_mask)
        gdi32.DeleteObject(hbm_color)


def _as_alpha_hcursor(img: Image.Image, hx: int, hy: int) -> int | None:
    img = img.convert("RGBA")
    w, h = img.size
    if w <= 0 or h <= 0 or w > 512 or h > 512:
        return None
    raw = img.tobytes("raw", "BGRA")

    bi = BITMAPV5HEADER()
    ctypes.memset(ctypes.byref(bi), 0, ctypes.sizeof(bi))
    bi.bV5Size = ctypes.sizeof(BITMAPV5HEADER)
    bi.bV5Width = w
    bi.bV5Height = -h
    bi.bV5Planes = 1
    bi.bV5BitCount = 32
    bi.bV5Compression = BI_BITFIELDS
    bi.bV5RedMask = 0x00FF0000
    bi.bV5GreenMask = 0x0000FF00
    bi.bV5BlueMask = 0x000000FF
    bi.bV5AlphaMask = 0xFF000000

    hdc = win32gui.GetDC(0)
    try:
        bits_ptr = ctypes.c_void_p()
        hbm_color = gdi32.CreateDIBSection(
            hdc, ctypes.byref(bi), DIB_RGB_COLORS, ctypes.byref(bits_ptr), None, 0
        )
        if not hbm_color or not bits_ptr:
            return None
        ctypes.memmove(bits_ptr, raw, len(raw))
    finally:
        win32gui.ReleaseDC(0, hdc)

    stride = ((w + 15) // 16) * 2
    mask = (ctypes.c_ubyte * (stride * h))()
    hbm_mask = gdi32.CreateBitmap(w, h, 1, 1, mask)
    if not hbm_mask:
        gdi32.DeleteObject(hbm_color)
        return None
    try:
        ii = ICONINFO()
        ii.fIcon = False
        ii.xHotspot = hx
        ii.yHotspot = hy
        ii.hbmMask = hbm_mask
        ii.hbmColor = hbm_color
        hcursor = user32.CreateIconIndirect(ctypes.byref(ii))
        return int(hcursor) if hcursor else None
    finally:
        gdi32.DeleteObject(hbm_mask)
        gdi32.DeleteObject(hbm_color)


def pil_to_hcursor(img: Image.Image, hotspot: tuple[int, int]) -> int | None:
    """
    Build an HCURSOR that displays correctly as a *hardware* cursor.

    Soft-alpha art (grab hand, etc.) → BITMAPV5 alpha.
    Binary / pure-black glyphs → classic color + AND mask (alpha bytes forced
    to 0). That is the path that does not turn Chrome resize/vertical-text
    into a solid black square.
    """
    try:
        img = img.convert("RGBA")
    except Exception:
        return None
    w, h = img.size
    if w <= 0 or h <= 0:
        return None
    if max(w, h) > _MAX_CURSOR_SIDE:
        img, hx, hy = _crop_to_content(img, int(hotspot[0]), int(hotspot[1]))
    else:
        hx = max(0, min(img.width - 1, int(hotspot[0])))
        hy = max(0, min(img.height - 1, int(hotspot[1])))

    # Pure-black Chrome glyphs must never take the alpha path — hardware cursors
    # paint (0,0,0,0) as opaque black when alpha mode engages. Soft-alpha color
    # art (grab hand, system arrow) uses BITMAPV5.
    if frame_is_pure_dark(img) or not _has_soft_alpha(img):
        return _as_masked_color_hcursor(img, hx, hy)
    hcursor = _as_alpha_hcursor(img, hx, hy)
    if hcursor:
        return hcursor
    return _as_masked_color_hcursor(img, hx, hy)


def create_blank_hcursor() -> int:
    h = pil_to_hcursor(Image.new("RGBA", (32, 32), (0, 0, 0, 0)), (0, 0))
    if not h:
        raise RuntimeError("Failed to create blank cursor")
    return h
