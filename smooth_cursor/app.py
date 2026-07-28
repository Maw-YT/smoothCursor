"""Main Smooth Cursor application."""

from __future__ import annotations

import atexit
import ctypes
import math
import signal
import sys
import threading
import time
from ctypes import wintypes

import win32api
import win32con
import win32gui
import win32process

from .anim import AnimState, on_click_down, on_click_up, on_key_type, on_scroll, update_anim
from .ani import (
    AniPlayer,
    load_scheme_slot,
    read_scheme_cursor_paths,
    try_load_ani,
)
from .build import draw_key_badge, pil_to_hcursor, rotate_around_hotspot
from .capture import (
    CursorFrame,
    frame_is_black_alpha,
    frame_is_usable,
    icon_to_cursor_frame,
    probe_cursor_passthrough,
)
from .physics import InertiaState, update_inertia
from .settings import Settings, load_settings
from .winapi import (
    CURSOR_SHOWING,
    CURSORINFO,
    HC_ACTION,
    HOOKPROC,
    KBDLLHOOKSTRUCT,
    LLKHF_UP,
    MSLLHOOKSTRUCT,
    OCR_TYPES,
    POINT,
    SPI_SETCURSORS,
    WH_KEYBOARD_LL,
    WH_MOUSE_LL,
    WM_KEYDOWN,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_MBUTTONDOWN,
    WM_MBUTTONUP,
    WM_MOUSEWHEEL,
    WM_RBUTTONDOWN,
    WM_RBUTTONUP,
    WM_SYSKEYDOWN,
    user32,
)

_attach_tid: int | None = None
_attach_our: int | None = None


def restore_cursor_visibility(max_steps: int = 256) -> None:
    """Undo any leftover ShowCursor(FALSE) balance so the desktop pointer returns."""
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    if user32.GetCursorInfo(ctypes.byref(ci)) and (ci.flags & CURSOR_SHOWING):
        return
    for _ in range(max_steps):
        if user32.ShowCursor(True) >= 0:
            break


def ensure_attached(*, force: bool = False) -> None:
    """Attach to the thread that owns the window under the cursor (for SetCursor)."""
    global _attach_tid, _attach_our
    our_tid = win32api.GetCurrentThreadId()
    _attach_our = our_tid
    if not force and _attach_tid is not None:
        return

    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    hwnd = user32.WindowFromPoint(pt)
    target_tid = our_tid
    if hwnd:
        target_tid = win32process.GetWindowThreadProcessId(hwnd)[0] or our_tid

    if _attach_tid is not None and _attach_tid != target_tid and _attach_tid != our_tid:
        user32.AttachThreadInput(our_tid, _attach_tid, False)
        _attach_tid = None

    if target_tid != our_tid and _attach_tid != target_tid:
        if user32.AttachThreadInput(our_tid, target_tid, True):
            _attach_tid = target_tid


def detach_cursor_thread() -> None:
    """Always detach — leaving AttachThreadInput on breaks clicks / desktop focus."""
    global _attach_tid, _attach_our
    if _attach_tid is not None and _attach_our is not None and _attach_tid != _attach_our:
        user32.AttachThreadInput(_attach_our, _attach_tid, False)
    _attach_tid = None


def force_set_cursor(hcursor: int) -> None:
    """SetCursor only sticks over other apps while input-attached."""
    ensure_attached(force=True)
    win32api.SetCursor(hcursor)


class SmoothCursorApp:
    """
    Hardware-cursor inertia + animation.

    - System cursors: loaded from the Windows scheme (.cur / .ani), posed via
      SetSystemCursor (desktop / taskbar / Start).
    - Scheme .ani (Wait, AppStarting, …) play frame-by-frame automatically.
    - App custom cursors (Chrome grab / I-beam / …): live capture + SetCursor.
    """

    PROBE_INTERVAL_S = 0.15
    PROBE_YIELD_S = 0.003

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = (settings or load_settings()).copy()
        self.physics = InertiaState()
        self.anim = AnimState()
        self._running = False
        self._modified = False
        self._ocr_frames: dict[int, CursorFrame] = {}
        self._handle_to_ocr: dict[int, int] = {}
        self._slot_angle: dict[int, tuple] = {}

        self._custom_frame: CursorFrame | None = None
        self._custom_fp: bytes | None = None
        self._custom_handle: int | None = None
        self._custom_hcursor: int | None = None
        self._custom_pose: tuple | None = None
        self._in_custom = False
        # Handles we permanently leave native (black+alpha / mono ink).
        # Once classified, never capture/rebuild again for that HCURSOR value.
        self._passthrough_handles: set[int] = set()
        # Handles already proven to be color (skip re-probe every frame).
        self._color_handles: set[int] = set()
        self._passthrough_attached = False
        self._capture_cache: dict[int, tuple[float, CursorFrame | None]] = {}
        # Short TTL so live animated HCURSORs (busy spinners, etc.) can advance.
        self._capture_cache_ttl = 0.045

        self._retired: list[tuple[float, int]] = []
        self._mouse_hook = None
        self._mouse_proc = None
        self._keyboard_hook = None
        self._keyboard_proc = None
        self._last_probe_at = 0.0
        self._scale = 1.0
        self._anim_rot = 0.0
        # Per-OCR animated players from the Windows cursor scheme (.ani files).
        self._ocr_ani: dict[int, AniPlayer] = {}
        # Optional user .ani override (Ani tab).
        self._ani_player: AniPlayer | None = None
        self._ani_loaded_path = ""
        # Cap system .ani republishes — SetSystemCursor every frame stalls the PC.
        self._ani_publish_interval_s = 1.0 / 12.0
        self._ani_last_publish_at: dict[int, float] = {}
        # Pre-baked HCURSOR templates per (ocr_id, frame_index) — CopyIcon + SetSystemCursor
        # instead of PIL rotate/encode on every spinner frame.
        self._ani_templates: dict[tuple[int, int], int] = {}
        self.apply_settings(self.settings)

    def apply_settings(self, settings: Settings) -> None:
        """Hot-apply settings while the engine is running."""
        self.settings = settings.copy()
        self.settings.apply_to_physics(self.physics)
        self.settings.apply_to_anim(self.anim)
        self._reload_ani()

    def _reload_ani(self, *, force: bool = False) -> None:
        """Optional user-picked .ani override for the arrow (or all slots)."""
        path = (self.settings.ani_path or "").strip()
        if not self.settings.ani_enabled or not path:
            self._ani_player = None
            self._ani_loaded_path = ""
            self._slot_angle.clear()
            return
        if not force and self._ani_player is not None and path == self._ani_loaded_path:
            return
        ani = try_load_ani(path)
        if ani is None:
            self._ani_player = None
            self._ani_loaded_path = ""
            return
        self._ani_player = AniPlayer(ani)
        self._ani_loaded_path = path
        self._slot_angle.clear()
        # Drop arrow override templates so they rebake for the new file.
        for key in [k for k in self._ani_templates if k[0] == win32con.OCR_NORMAL]:
            try:
                user32.DestroyIcon(self._ani_templates.pop(key))
            except Exception:
                self._ani_templates.pop(key, None)
        self._bake_ani_templates(win32con.OCR_NORMAL, self._ani_player)

    def stop(self) -> None:
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def _cached_capture(self, handle: int) -> CursorFrame | None:
        now = time.perf_counter()
        hit = self._capture_cache.get(handle)
        if hit is not None and now - hit[0] < self._capture_cache_ttl:
            return hit[1]
        frame = icon_to_cursor_frame(handle)
        self._capture_cache[handle] = (now, frame)
        if len(self._capture_cache) > 64:
            oldest = min(self._capture_cache.items(), key=lambda kv: kv[1][0])[0]
            self._capture_cache.pop(oldest, None)
        return frame

    def _snapshot_system_cursors(self) -> None:
        """
        Load system cursor art from the active Windows scheme:
        - .ani → AniPlayer (animated Wait / AppStarting / …)
        - .cur → static CursorFrame
        Falls back to LoadCursor snapshot when the scheme path is missing.
        """
        self._ocr_frames.clear()
        self._ocr_ani.clear()
        self._clear_ani_templates()
        scheme = read_scheme_cursor_paths()
        for ocr_id in OCR_TYPES:
            path = scheme.get(ocr_id)
            if path is not None:
                static, player = load_scheme_slot(path)
                if player is not None:
                    self._ocr_ani[ocr_id] = player
                    cur = player.current()
                    if cur is not None:
                        self._ocr_frames[ocr_id] = cur
                    self._bake_ani_templates(ocr_id, player)
                    continue
                if static is not None:
                    self._ocr_frames[ocr_id] = static
                    continue
            # Fallback: one-shot capture of the live system cursor.
            frame = icon_to_cursor_frame(win32gui.LoadCursor(0, ocr_id))
            if frame is not None and frame_is_usable(frame):
                self._ocr_frames[ocr_id] = frame

    def _remap_handles(self) -> None:
        self._handle_to_ocr.clear()
        for ocr_id in OCR_TYPES:
            self._handle_to_ocr[int(win32gui.LoadCursor(0, ocr_id))] = ocr_id

    def _remap_one(self, ocr_id: int) -> None:
        """Refresh only one OCR→handle mapping after SetSystemCursor."""
        stale = [h for h, oid in self._handle_to_ocr.items() if oid == ocr_id]
        for h in stale:
            del self._handle_to_ocr[h]
        self._handle_to_ocr[int(win32gui.LoadCursor(0, ocr_id))] = ocr_id

    def _retire(self, hcursor: int | None) -> None:
        if hcursor:
            self._retired.append((time.perf_counter(), int(hcursor)))

    def _gc_retired(self) -> None:
        now = time.perf_counter()
        keep: list[tuple[float, int]] = []
        for when, h in self._retired:
            if now - when > 1.5:
                try:
                    user32.DestroyIcon(h)
                except Exception:
                    pass
            else:
                keep.append((when, h))
        self._retired = keep

    def _clear_ani_templates(self) -> None:
        for h in self._ani_templates.values():
            try:
                user32.DestroyIcon(h)
            except Exception:
                pass
        self._ani_templates.clear()

    def _bake_ani_templates(self, ocr_id: int, player: AniPlayer) -> None:
        """Encode each .ani frame once as an HCURSOR template."""
        for i, fr in enumerate(player.ani.frames):
            key = (ocr_id, i)
            if key in self._ani_templates:
                continue
            try:
                h = pil_to_hcursor(fr.image, fr.hotspot)
            except Exception:
                h = None
            if h:
                self._ani_templates[key] = int(h)

    def _publish_ani_fast(self, ocr_id: int, player: AniPlayer, pose: tuple) -> bool:
        """
        Advance a visible .ani with CopyIcon + SetSystemCursor only.
        No per-frame PIL rotate/encode (that was the visible-ani lag).
        """
        self._bake_ani_templates(ocr_id, player)
        key = (ocr_id, int(player.frame_index))
        template = self._ani_templates.get(key)
        if not template:
            return False
        now = time.perf_counter()
        last = self._ani_last_publish_at.get(ocr_id, 0.0)
        prev = self._slot_angle.get(ocr_id)
        if prev == pose:
            return True
        # Frame-only updates: throttle. First paint / badge-off always allowed.
        if prev is not None and prev[-1] != pose[-1] and now - last < self._ani_publish_interval_s:
            return True
        copy = user32.CopyIcon(template)
        if not copy:
            return False
        if not user32.SetSystemCursor(copy, ocr_id):
            user32.DestroyIcon(copy)
            return False
        self._ani_last_publish_at[ocr_id] = now
        self._slot_angle[ocr_id] = pose
        self._modified = True
        self._remap_one(ocr_id)
        return True

    def _override_ani_frame(self, ocr_id: int) -> CursorFrame | None:
        """User .ani override from the Ani settings tab."""
        if self._ani_player is None or not self.settings.ani_enabled:
            return None
        if self.settings.ani_replace_all or ocr_id == win32con.OCR_NORMAL:
            return self._ani_player.current()
        return None

    def _slot_ani_player(self, ocr_id: int) -> AniPlayer | None:
        if self._ani_player is not None and self.settings.ani_enabled:
            if self.settings.ani_replace_all or ocr_id == win32con.OCR_NORMAL:
                return self._ani_player
        return self._ocr_ani.get(ocr_id)

    def _frame_for_ocr(self, ocr_id: int) -> CursorFrame | None:
        """Best current art for a system OCR slot (override → scheme ani → static)."""
        override = self._override_ani_frame(ocr_id)
        if override is not None:
            return override
        player = self._ocr_ani.get(ocr_id)
        if player is not None:
            return player.current()
        return self._ocr_frames.get(ocr_id)

    def _pose(self, angle: float, *, ocr_id: int | None = None) -> tuple:
        badge = self.anim.badge_label if self.anim.badge_alpha > 0.05 else ""
        badge_a = float(round(self.anim.badge_alpha * 10) / 10)
        badge_n = int(self.anim.badge_count) if badge else 1
        style = self.settings.badge_style
        theme = self.settings.badge_theme
        font = self.settings.badge_font
        bsize = float(round(self.settings.badge_size * 20) / 20)
        ani_i = -1
        player = self._slot_ani_player(ocr_id) if ocr_id is not None else None
        if player is not None:
            ani_i = player.frame_index
            # Animated system cursors: freeze tilt/scale so we can CopyIcon frames
            # instead of rebuilding a rotated sprite on every mouse move.
            draw = 0.0
            scale = 1.0
        else:
            draw = float(round(angle + self._anim_rot))
            scale = float(round(self._scale * 20) / 20)
        return draw, scale, badge, badge_a, badge_n, style, theme, font, bsize, ani_i

    def _compose_sprite(
        self, image, hx: int, hy: int, angle: float, scale: float
    ):
        sprite, hx2, hy2 = rotate_around_hotspot(image, hx, hy, angle, scale)
        if self.settings.typing_enabled and self.anim.badge_label and self.anim.badge_alpha > 0.05:
            sprite, hx2, hy2 = draw_key_badge(
                sprite,
                hx2,
                hy2,
                self.anim.badge_label,
                self.anim.badge_alpha,
                style=self.settings.badge_style,
                theme=self.settings.badge_theme,
                size=self.settings.badge_size,
                font_family=self.settings.badge_font,
                count=self.anim.badge_count,
            )
        return sprite, hx2, hy2

    def _publish_one(self, ocr_id: int, angle: float) -> None:
        frame = self._frame_for_ocr(ocr_id)
        if frame is None:
            return
        pose = self._pose(angle, ocr_id=ocr_id)
        prev = self._slot_angle.get(ocr_id)
        if prev == pose:
            return

        player = self._slot_ani_player(ocr_id)
        # Fast path: visible .ani with no badge → swap pre-baked frames only.
        if player is not None and not pose[2]:
            if self._publish_ani_fast(ocr_id, player, pose):
                return

        # Slow path (static cursors, or .ani while a typing badge is showing).
        if (
            prev is not None
            and player is not None
            and prev[:-1] == pose[:-1]
            and prev[-1] != pose[-1]
        ):
            now = time.perf_counter()
            last = self._ani_last_publish_at.get(ocr_id, 0.0)
            if now - last < self._ani_publish_interval_s:
                return
            self._ani_last_publish_at[ocr_id] = now
        sprite, hx, hy = self._compose_sprite(
            frame.image, frame.hotspot[0], frame.hotspot[1], pose[0], pose[1]
        )
        hcursor = pil_to_hcursor(sprite, (hx, hy))
        if not hcursor:
            return
        if not user32.SetSystemCursor(hcursor, ocr_id):
            user32.DestroyIcon(hcursor)
            return
        self._slot_angle[ocr_id] = pose
        self._modified = True
        self._remap_one(ocr_id)

    def _tick_animated_system(
        self, angle: float, dt: float, active_ocr: int | None
    ) -> None:
        """
        Advance scheme + override .ani players in memory.

        Only invalidate the *visible* OCR slot so the main loop republishes it.
        Never SetSystemCursor for hidden Wait/Busy players (that caused the lag).
        """
        speed = float(self.settings.ani_speed) if self.settings.ani_enabled else 1.0
        dirty: set[int] = set()

        if self._ani_player is not None and self.settings.ani_enabled:
            if self._ani_player.update(dt, speed):
                if self.settings.ani_replace_all:
                    dirty.update(self._ocr_frames)
                else:
                    dirty.add(win32con.OCR_NORMAL)

        for ocr_id, player in self._ocr_ani.items():
            if self._override_ani_frame(ocr_id) is not None:
                continue
            if player.update(dt, 1.0):
                cur = player.current()
                if cur is not None:
                    self._ocr_frames[ocr_id] = cur
                dirty.add(ocr_id)

        if active_ocr is not None and active_ocr in dirty:
            self._slot_angle.pop(active_ocr, None)

    def _publish_all(self, angle: float) -> None:
        for ocr_id in list(self._ocr_frames):
            self._slot_angle.pop(ocr_id, None)
            self._publish_one(ocr_id, angle)

    def _mark_passthrough(self, handle: int) -> None:
        if handle:
            self._passthrough_handles.add(int(handle))
            if len(self._passthrough_handles) > 256:
                self._passthrough_handles.clear()
                self._passthrough_handles.add(int(handle))

    def _is_passthrough(self, handle: int) -> bool:
        return int(handle) in self._passthrough_handles

    def _passthrough_native(self, handle: int) -> None:
        """Leave black+alpha / mono ink cursors alone (no capture, no rebuild)."""
        self._mark_passthrough(handle)
        if self._in_custom:
            self._exit_custom()
        elif self._passthrough_attached:
            detach_cursor_thread()
            self._passthrough_attached = False

    def _enter_or_refresh_hw_custom(self, handle: int, angle: float) -> None:
        """Pose live app cursors (Chrome, etc.). Black+alpha are never rebuilt."""
        try:
            if self._is_passthrough(handle):
                self._passthrough_native(handle)
                return

            # Cheap preflight BEFORE full capture (dual-background was the lag).
            # Skip once we know this handle is a real color cursor.
            if int(handle) not in self._color_handles:
                if probe_cursor_passthrough(handle):
                    self._passthrough_native(handle)
                    return
                self._color_handles.add(int(handle))
                if len(self._color_handles) > 256:
                    self._color_handles.clear()
                    self._color_handles.add(int(handle))

            if self._custom_hcursor is not None and handle == self._custom_hcursor:
                self._ensure_hw_custom(angle)
                return

            # Same app handle we already own — still refresh art (animated cursors).
            if self._in_custom and handle == self._custom_handle and self._custom_frame is not None:
                frame = self._cached_capture(handle)
                if frame is not None and frame_is_black_alpha(frame):
                    self._passthrough_native(handle)
                    return
                if frame is not None and frame_is_usable(frame):
                    if self._custom_fp is None or self._custom_fp != frame.fingerprint:
                        self._commit_hw_custom(frame, handle, angle)
                        return
                self._ensure_hw_custom(angle)
                return

            frame = self._cached_capture(handle)
            if frame is not None and frame_is_black_alpha(frame):
                self._passthrough_native(handle)
                return
            if frame is None or not frame_is_usable(frame):
                if (
                    self._in_custom
                    and handle != self._custom_handle
                    and handle != self._custom_hcursor
                ):
                    self._exit_custom()
                elif self._in_custom and self._custom_frame is not None:
                    self._ensure_hw_custom(angle)
                return

            if not self._in_custom:
                self._commit_hw_custom(frame, handle, angle)
                return
            if self._custom_fp is not None and self._custom_fp == frame.fingerprint:
                self._custom_handle = handle
                self._ensure_hw_custom(angle)
                return
            self._commit_hw_custom(frame, handle, angle)
        except Exception:
            try:
                self._exit_custom()
            except Exception:
                pass

    def _commit_hw_custom(self, frame: CursorFrame, handle: int, angle: float) -> None:
        if frame_is_black_alpha(frame):
            self._passthrough_native(handle)
            return
        self._custom_frame = frame
        self._custom_fp = frame.fingerprint
        self._custom_handle = handle
        self._custom_pose = None
        self._in_custom = True
        self._ensure_hw_custom(angle)

    def _ensure_hw_custom(self, angle: float) -> None:
        assert self._custom_frame is not None
        try:
            # Chrome / app cursors: never bake .ani frame index into pose.
            pose = self._pose(angle, ocr_id=None)
            if self._custom_pose == pose and self._custom_hcursor:
                force_set_cursor(self._custom_hcursor)
                return
            sprite, hx, hy = self._compose_sprite(
                self._custom_frame.image,
                self._custom_frame.hotspot[0],
                self._custom_frame.hotspot[1],
                pose[0],
                pose[1],
            )
            hcursor = pil_to_hcursor(sprite, (hx, hy))
            if not hcursor:
                return
            old = self._custom_hcursor
            self._custom_hcursor = hcursor
            self._custom_pose = pose
            force_set_cursor(hcursor)
            self._retire(old)
            self._gc_retired()
        except Exception:
            pass

    def _exit_custom(self) -> None:
        self._in_custom = False
        self._custom_frame = None
        self._custom_fp = None
        self._custom_handle = None
        self._custom_pose = None
        old = self._custom_hcursor
        self._custom_hcursor = None
        self._retire(old)
        detach_cursor_thread()

    def _read_cursor(self) -> tuple[int, int, int, bool]:
        ci = CURSORINFO()
        ci.cbSize = ctypes.sizeof(CURSORINFO)
        if not user32.GetCursorInfo(ctypes.byref(ci)):
            return 0, 0, 0, False
        showing = (ci.flags & CURSOR_SHOWING) != 0
        h = int(ci.hCursor) if ci.hCursor else 0
        return int(ci.ptScreenPos.x), int(ci.ptScreenPos.y), h, showing

    def _probe_app_cursor(self, now: float) -> int:
        """Detach briefly so Chrome can reclaim arrow/hand / swap grab↔grabbing."""
        if not self._in_custom:
            return 0
        if now - self._last_probe_at < self.PROBE_INTERVAL_S:
            return 0
        self._last_probe_at = now
        detach_cursor_thread()
        time.sleep(self.PROBE_YIELD_S)
        _, _, h, showing = self._read_cursor()
        if not showing or not h:
            return 0
        return h

    def _on_mouse_ll(self, nCode, wParam, lParam):
        if nCode != HC_ACTION:
            return user32.CallNextHookEx(self._mouse_hook, nCode, wParam, lParam)
        if not self.settings.anim_enabled:
            return user32.CallNextHookEx(self._mouse_hook, nCode, wParam, lParam)
        if wParam in (WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN):
            on_click_down(self.anim)
        elif wParam in (WM_LBUTTONUP, WM_RBUTTONUP, WM_MBUTTONUP):
            on_click_up(self.anim)
        elif wParam == WM_MOUSEWHEEL:
            info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            delta = ctypes.c_short(info.mouseData >> 16).value
            on_scroll(self.anim, self.physics, int(delta))
        return user32.CallNextHookEx(self._mouse_hook, nCode, wParam, lParam)

    def _on_keyboard_ll(self, nCode, wParam, lParam):
        if nCode != HC_ACTION:
            return user32.CallNextHookEx(self._keyboard_hook, nCode, wParam, lParam)
        if (
            self.settings.anim_enabled
            and self.settings.typing_enabled
            and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
        ):
            info = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if (info.flags & LLKHF_UP) == 0:
                on_key_type(self.anim, int(info.vkCode))
        return user32.CallNextHookEx(self._keyboard_hook, nCode, wParam, lParam)

    def _install_mouse_hook(self) -> None:
        self._mouse_proc = HOOKPROC(self._on_mouse_ll)
        self._mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc, None, 0)

    def _install_keyboard_hook(self) -> None:
        self._keyboard_proc = HOOKPROC(self._on_keyboard_ll)
        self._keyboard_hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._keyboard_proc, None, 0
        )

    def _uninstall_mouse_hook(self) -> None:
        if self._mouse_hook:
            user32.UnhookWindowsHookEx(self._mouse_hook)
            self._mouse_hook = None
        self._mouse_proc = None

    def _uninstall_keyboard_hook(self) -> None:
        if self._keyboard_hook:
            user32.UnhookWindowsHookEx(self._keyboard_hook)
            self._keyboard_hook = None
        self._keyboard_proc = None

    def _restore(self) -> None:
        self._uninstall_mouse_hook()
        self._uninstall_keyboard_hook()
        self._exit_custom()
        detach_cursor_thread()
        restore_cursor_visibility()
        for _, h in self._retired:
            try:
                user32.DestroyIcon(h)
            except Exception:
                pass
        self._retired.clear()
        self._capture_cache.clear()
        self._passthrough_handles.clear()
        self._color_handles.clear()
        self._passthrough_attached = False
        self._clear_ani_templates()
        if self._modified:
            user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)
            self._modified = False
        try:
            win32api.SetCursor(win32gui.LoadCursor(0, win32con.IDC_ARROW))
        except Exception:
            pass

    def run(self, *, quiet: bool = False) -> None:
        if not quiet:
            print("Smooth Cursor running.", flush=True)
            print("Press Ctrl+C in this window to quit.", flush=True)

        restore_cursor_visibility()
        user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)
        self._snapshot_system_cursors()
        if not self._ocr_frames:
            if not quiet:
                print("Failed to capture system cursors.", flush=True)
                sys.exit(1)
            raise RuntimeError("Failed to capture system cursors.")
        self._publish_all(0.0)
        self._install_mouse_hook()
        self._install_keyboard_hook()
        if not getattr(self, "_atexit_registered", False):
            atexit.register(self._restore)
            self._atexit_registered = True

        # Signal / console handlers only work reliably on the main thread (CLI mode).
        if threading.current_thread() is threading.main_thread():
            def _stop(*_args) -> None:
                self._running = False

            signal.signal(signal.SIGINT, _stop)
            if hasattr(signal, "SIGBREAK"):
                signal.signal(signal.SIGBREAK, _stop)

            HandlerRoutine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

            @HandlerRoutine
            def _console_handler(ctrl_type: int) -> bool:
                self._restore()
                self._running = False
                return False

            self._console_handler_ref = _console_handler
            ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler, True)

        self._running = True
        prev = time.perf_counter()

        try:
            while self._running:
                win32gui.PumpWaitingMessages()
                now = time.perf_counter()
                dt = now - prev
                prev = now

                x, y, h, showing = self._read_cursor()
                if not showing:
                    if self._in_custom:
                        self._exit_custom()
                    else:
                        detach_cursor_thread()
                    if self.settings.inertia_enabled:
                        update_inertia(self.physics, float(x), float(y), dt)
                    time.sleep(0.002)
                    continue

                if self.settings.inertia_enabled:
                    angle = update_inertia(self.physics, float(x), float(y), dt)
                else:
                    if self.physics.last_x is not None and dt > 0:
                        self.physics.last_vx = (float(x) - self.physics.last_x) / dt
                        self.physics.last_vy = (float(y) - self.physics.last_y) / dt
                    self.physics.angle_deg = 0.0
                    self.physics.angular_vel = 0.0
                    self.physics.last_x, self.physics.last_y = float(x), float(y)
                    angle = 0.0

                speed = math.hypot(self.physics.last_vx, self.physics.last_vy)
                speed_for_scale = speed if self.settings.inertia_enabled else 0.0
                if (
                    self.settings.anim_enabled
                    or self.settings.typing_enabled
                    or self.settings.inertia_enabled
                ):
                    self._scale, self._anim_rot = update_anim(
                        self.anim, dt, speed=speed_for_scale
                    )
                else:
                    self.anim.scale = 1.0
                    self.anim.scale_vel = 0.0
                    self.anim.rot = 0.0
                    self.anim.rot_vel = 0.0
                    self.anim.pressed = False
                    self.anim.badge_alpha = 0.0
                    self.anim.badge_label = ""
                    self.anim.badge_count = 1
                    self._scale, self._anim_rot = 1.0, 0.0

                # Resolve which system OCR (if any) is showing before ani tick,
                # so we only SetSystemCursor for the visible animated slot.
                active_ocr: int | None = None
                if not (
                    self._custom_hcursor is not None and h == self._custom_hcursor
                ):
                    active_ocr = self._handle_to_ocr.get(h)

                self._tick_animated_system(angle, dt, active_ocr)

                probed = self._probe_app_cursor(now)
                if probed:
                    h = probed
                    active_ocr = None
                if not h:
                    time.sleep(0.001)
                    continue

                try:
                    if self._is_passthrough(h):
                        self._passthrough_native(h)
                        time.sleep(0.003)
                        continue

                    if self._custom_hcursor is not None and h == self._custom_hcursor:
                        if self._in_custom and self._custom_frame is not None:
                            self._ensure_hw_custom(angle)
                        time.sleep(0.001)
                        continue

                    ocr_id = self._handle_to_ocr.get(h)
                    if ocr_id is not None:
                        if self._in_custom:
                            self._exit_custom()
                        else:
                            detach_cursor_thread()
                        self._publish_one(ocr_id, angle)
                    else:
                        # Live app cursor (Chrome grab, I-beam, …) — capture & pose.
                        # Black+alpha / mono ink: cheap probe → permanent passthrough.
                        self._enter_or_refresh_hw_custom(h, angle)
                except Exception:
                    try:
                        self._exit_custom()
                    except Exception:
                        pass

                time.sleep(0.001)
        except KeyboardInterrupt:
            if not quiet:
                print("\nExiting...", flush=True)
        finally:
            self._restore()
            if not quiet:
                print("Cursors restored.", flush=True)


def main(*, gui: bool = True) -> None:
    if sys.platform != "win32":
        print("Smooth Cursor only supports Windows.")
        sys.exit(1)
    restore_cursor_visibility()
    user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)
    if gui:
        from .gui import run_gui

        run_gui()
        return
    SmoothCursorApp(load_settings()).run()
