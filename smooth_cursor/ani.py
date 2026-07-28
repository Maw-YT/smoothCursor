"""Load and play Windows animated cursors (.ani)."""

from __future__ import annotations

import ctypes
import os
import struct
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

import win32con
from PIL import Image

from .capture import CursorFrame, _fingerprint, frame_is_usable, icon_to_cursor_frame
from .winapi import user32

AF_ICON = 0x1
AF_SEQUENCE = 0x2
_JIFFY_S = 1.0 / 60.0


@dataclass
class AniCursor:
    path: str
    title: str = ""
    frames: list[CursorFrame] = field(default_factory=list)
    # Playback steps: (frame_index, duration_seconds)
    steps: list[tuple[int, float]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.frames) and bool(self.steps)


class AniPlayer:
    """Advances through AniCursor steps based on elapsed time."""

    def __init__(self, ani: AniCursor) -> None:
        self.ani = ani
        self.step_i = 0
        self.accum = 0.0
        self._frame_i = ani.steps[0][0] if ani.steps else 0

    def current(self) -> CursorFrame | None:
        if not self.ani.frames:
            return None
        idx = max(0, min(len(self.ani.frames) - 1, self._frame_i))
        return self.ani.frames[idx]

    @property
    def frame_index(self) -> int:
        return self._frame_i

    def update(self, dt: float, speed: float = 1.0) -> bool:
        """Advance animation. Returns True if the visible frame changed."""
        if not self.ani.steps:
            return False
        speed = max(0.05, min(4.0, float(speed)))
        self.accum += max(0.0, dt) * speed
        changed = False
        # Cap catch-up so long stalls don't spin forever.
        for _ in range(64):
            _, dur = self.ani.steps[self.step_i]
            dur = max(1.0 / 120.0, dur)
            if self.accum < dur:
                break
            self.accum -= dur
            self.step_i = (self.step_i + 1) % len(self.ani.steps)
            new_i = self.ani.steps[self.step_i][0]
            if new_i != self._frame_i:
                self._frame_i = new_i
                changed = True
        return changed


def _read_fourcc(data: bytes, off: int) -> str:
    return data[off : off + 4].decode("ascii", errors="replace")


def _walk_chunks(data: bytes, start: int, end: int):
    """Yield (id, payload_bytes, absolute_payload_offset)."""
    off = start
    while off + 8 <= end:
        cid = _read_fourcc(data, off)
        size = struct.unpack_from("<I", data, off + 4)[0]
        payload_off = off + 8
        payload_end = payload_off + size
        if payload_end > end:
            break
        yield cid, data[payload_off:payload_end], payload_off
        # RIFF chunks are word-aligned.
        off = payload_end + (size & 1)


def system_cursor_side() -> int:
    """Windows cursor display size (CursorBaseSize / SM_CXCURSOR), typically 32."""
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Cursors")
        try:
            val, _ = winreg.QueryValueEx(key, "CursorBaseSize")
            side = int(val)
            if 16 <= side <= 256:
                return side
        finally:
            winreg.CloseKey(key)
    except Exception:
        pass
    try:
        side = int(user32.GetSystemMetrics(13))  # SM_CXCURSOR
        if side > 0:
            return max(16, min(256, side))
    except Exception:
        pass
    return 32


def scale_cursor_frame(frame: CursorFrame, side: int) -> CursorFrame:
    """Downscale a frame so max(side) matches the system cursor size."""
    side = max(16, min(256, int(side)))
    w, h = frame.image.size
    if w <= 0 or h <= 0:
        return frame
    if max(w, h) <= side + 1:
        return frame
    scale = side / float(max(w, h))
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    img = frame.image.resize((nw, nh), Image.Resampling.LANCZOS)
    hx = max(0, min(nw - 1, int(round(frame.hotspot[0] * scale))))
    hy = max(0, min(nh - 1, int(round(frame.hotspot[1] * scale))))
    return CursorFrame(img, (hx, hy), _fingerprint(img))


def _decode_icon_chunk(payload: bytes) -> CursorFrame | None:
    """Decode a CUR/ICO resource blob into a CursorFrame."""
    if not payload or len(payload) < 6:
        return None

    target = system_cursor_side()

    # Prefer Win32 — preserves CUR hotspots; request system display size.
    try:
        buf = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        # 0x00030000 = version; fIcon=False → cursor
        hicon = user32.CreateIconFromResourceEx(
            buf, len(payload), False, 0x00030000, target, target, 0
        )
        if not hicon:
            # Retry at native size, then scale ourselves.
            hicon = user32.CreateIconFromResourceEx(
                buf, len(payload), False, 0x00030000, 0, 0, 0
            )
        if hicon:
            try:
                frame = icon_to_cursor_frame(int(hicon))
                if frame is not None and frame_is_usable(frame):
                    return scale_cursor_frame(frame, target)
            finally:
                user32.DestroyIcon(hicon)
    except Exception:
        pass

    # Fallback: Pillow ICO (hotspot may be wrong for CUR).
    try:
        img = Image.open(BytesIO(payload))
        img.load()
        # Prefer the size closest to the system cursor (not the largest).
        if getattr(img, "n_frames", 1) > 1:
            best = None
            best_score = None
            for i in range(img.n_frames):
                img.seek(i)
                area = img.width * img.height
                score = abs(max(img.width, img.height) - target)
                if best_score is None or score < best_score or (
                    score == best_score and area < (best.width * best.height if best else 10**9)
                ):
                    best_score = score
                    best = img.copy().convert("RGBA")
            rgba = best
        else:
            rgba = img.convert("RGBA")
        if rgba is None:
            return None
        hx = max(0, rgba.width // 8)
        hy = max(0, rgba.height // 8)
        # CUR directory stores hotspot in planes/bitcount of first entry.
        if len(payload) >= 22 and payload[2:4] == b"\x02\x00":
            hx = int.from_bytes(payload[10:12], "little")
            hy = int.from_bytes(payload[12:14], "little")
            hx = max(0, min(rgba.width - 1, hx))
            hy = max(0, min(rgba.height - 1, hy))
        frame = CursorFrame(rgba, (hx, hy), _fingerprint(rgba))
        if frame_is_usable(frame):
            return scale_cursor_frame(frame, target)
    except Exception:
        pass
    return None


def load_ani(path: str | Path) -> AniCursor:
    """Parse a Windows .ani file into frames + timed steps."""
    path = Path(path)
    raw = path.read_bytes()
    if len(raw) < 12 or raw[0:4] != b"RIFF" or raw[8:12] != b"ACON":
        raise ValueError("Not a valid ANI (RIFF ACON) file")

    riff_size = struct.unpack_from("<I", raw, 4)[0]
    end = min(len(raw), 8 + riff_size)

    title = ""
    c_frames = 0
    c_steps = 0
    default_jiffies = 1
    flags = AF_ICON
    rates: list[int] | None = None
    seq: list[int] | None = None
    icon_payloads: list[bytes] = []

    for cid, payload, _poff in _walk_chunks(raw, 12, end):
        if cid == "LIST":
            if len(payload) < 4:
                continue
            list_type = payload[0:4]
            for scid, spayload, _ in _walk_chunks(payload, 4, len(payload)):
                if list_type == b"INFO" and scid == "INAM":
                    title = spayload.split(b"\x00", 1)[0].decode("latin-1", errors="replace").strip()
                elif list_type == b"fram" and scid == "icon":
                    icon_payloads.append(spayload)
        elif cid == "anih":
            if len(payload) < 36:
                raise ValueError("Truncated anih header")
            (
                _cb,
                c_frames,
                c_steps,
                _cx,
                _cy,
                _bits,
                _planes,
                default_jiffies,
                flags,
            ) = struct.unpack_from("<9I", payload, 0)
        elif cid == "rate":
            n = len(payload) // 4
            rates = list(struct.unpack_from(f"<{n}I", payload, 0))
        elif cid == "seq ":
            n = len(payload) // 4
            seq = list(struct.unpack_from(f"<{n}I", payload, 0))

    if not icon_payloads:
        raise ValueError("ANI has no icon frames")

    frames: list[CursorFrame] = []
    target = system_cursor_side()
    for blob in icon_payloads:
        frame = _decode_icon_chunk(blob)
        if frame is None:
            # Keep a transparent placeholder so indices stay aligned.
            blank = Image.new("RGBA", (target, target), (0, 0, 0, 0))
            frames.append(CursorFrame(blank, (0, 0), _fingerprint(blank)))
        else:
            frames.append(scale_cursor_frame(frame, target))

    if not any(frame_is_usable(f) for f in frames):
        raise ValueError("ANI frames could not be decoded")

    if c_frames <= 0:
        c_frames = len(frames)
    if c_steps <= 0:
        c_steps = c_frames
    if default_jiffies <= 0:
        default_jiffies = 1

    if seq is None or not (flags & AF_SEQUENCE):
        seq = list(range(min(c_steps, len(frames))))
        if len(seq) < c_steps:
            # Repeat last / pad
            while len(seq) < c_steps:
                seq.append(seq[-1] if seq else 0)
    else:
        seq = [max(0, min(len(frames) - 1, int(i))) for i in seq[:c_steps]]
        if not seq:
            seq = [0]

    if rates is None:
        rates = [default_jiffies] * len(seq)
    else:
        rates = list(rates[: len(seq)])
        while len(rates) < len(seq):
            rates.append(default_jiffies)

    steps = [
        (seq[i], max(1, int(rates[i])) * _JIFFY_S)
        for i in range(len(seq))
    ]

    return AniCursor(
        path=str(path.resolve()),
        title=title or path.stem,
        frames=frames,
        steps=steps,
    )


def try_load_ani(path: str | Path | None) -> AniCursor | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        ani = load_ani(p)
        return ani if ani.ok else None
    except Exception:
        return None


def load_cur_frame(path: str | Path) -> CursorFrame | None:
    """Load a static .cur / .ico file as a CursorFrame."""
    p = Path(os.path.expandvars(str(path)))
    if not p.is_file():
        return None
    try:
        h = user32.LoadCursorFromFileW(str(p))
        if not h:
            # Fallback: treat file bytes as CUR resource
            return _decode_icon_chunk(p.read_bytes())
        try:
            frame = icon_to_cursor_frame(int(h))
            return frame if frame is not None and frame_is_usable(frame) else None
        finally:
            user32.DestroyIcon(h)
    except Exception:
        try:
            return _decode_icon_chunk(p.read_bytes())
        except Exception:
            return None


# Registry value name → OCR_* id for the active Windows cursor scheme.
OCR_SCHEME_NAMES: dict[int, str] = {
    win32con.OCR_NORMAL: "Arrow",
    win32con.OCR_IBEAM: "IBeam",
    win32con.OCR_WAIT: "Wait",
    win32con.OCR_CROSS: "Crosshair",
    win32con.OCR_UP: "UpArrow",
    win32con.OCR_SIZENWSE: "SizeNWSE",
    win32con.OCR_SIZENESW: "SizeNESW",
    win32con.OCR_SIZEWE: "SizeWE",
    win32con.OCR_SIZENS: "SizeNS",
    win32con.OCR_SIZEALL: "SizeAll",
    win32con.OCR_NO: "No",
    win32con.OCR_HAND: "Hand",
    win32con.OCR_APPSTARTING: "AppStarting",
}


def read_scheme_cursor_paths() -> dict[int, Path]:
    """Map OCR id → file path from HKCU\\Control Panel\\Cursors."""
    import winreg

    out: dict[int, Path] = {}
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Cursors")
    except OSError:
        return out
    try:
        name_to_ocr = {v: k for k, v in OCR_SCHEME_NAMES.items()}
        i = 0
        while True:
            try:
                name, val, _typ = winreg.EnumValue(key, i)
                i += 1
            except OSError:
                break
            if name not in name_to_ocr or not val or not isinstance(val, str):
                continue
            path = Path(os.path.expandvars(val))
            if path.is_file():
                out[name_to_ocr[name]] = path
    finally:
        winreg.CloseKey(key)
    return out


def load_scheme_slot(path: Path) -> tuple[CursorFrame | None, AniPlayer | None]:
    """
    Load one scheme cursor file.
    Returns (static_frame, ani_player) — exactly one side is set when successful.
    """
    suffix = path.suffix.lower()
    if suffix == ".ani":
        ani = try_load_ani(path)
        if ani is not None:
            return None, AniPlayer(ani)
        return None, None
    frame = load_cur_frame(path)
    if frame is not None:
        return frame, None
    return None, None
