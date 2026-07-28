"""Click / scroll / typing cursor animations + speed scale."""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass

from .physics import InertiaState

user32 = ctypes.windll.user32

# Modifier / lock keys — alone they don't show a badge.
_MODIFIER_VKS = frozenset(
    {
        0x10,
        0x11,
        0x12,
        0xA0,
        0xA1,
        0xA2,
        0xA3,
        0xA4,
        0xA5,
        0x5B,
        0x5C,
        0x14,
        0x90,
        0x91,
    }
)

_SPECIAL_LABELS: dict[int, str] = {
    0x08: "Bksp",
    0x09: "Tab",
    0x0D: "Enter",
    0x1B: "Esc",
    0x20: "Space",
    0x2E: "Del",
    0x25: "←",
    0x26: "↑",
    0x27: "→",
    0x28: "↓",
    0x21: "PgUp",
    0x22: "PgDn",
    0x23: "End",
    0x24: "Home",
    0x2D: "Ins",
}

# OEM / punct keys — badge shows the typed glyph (Shift+. → ">").
_OEM_UNSHIFTED: dict[int, str] = {
    0xBA: ";",
    0xBB: "=",
    0xBC: ",",
    0xBD: "-",
    0xBE: ".",
    0xBF: "/",
    0xC0: "`",
    0xDB: "[",
    0xDC: "\\",
    0xDD: "]",
    0xDE: "'",
}

# US-layout Shift fallbacks when ToUnicode is unavailable in the hook.
_US_SHIFT_GLYPHS: dict[int, str] = {
    0x30: ")",
    0x31: "!",
    0x32: "@",
    0x33: "#",
    0x34: "$",
    0x35: "%",
    0x36: "^",
    0x37: "&",
    0x38: "*",
    0x39: "(",
    0xBA: ":",
    0xBB: "+",
    0xBC: "<",
    0xBD: "_",
    0xBE: ">",
    0xBF: "?",
    0xC0: "~",
    0xDB: "{",
    0xDC: "|",
    0xDD: "}",
    0xDE: '"',
}


def _key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def _shift_down() -> bool:
    return _key_down(0x10) or _key_down(0xA0) or _key_down(0xA1)


def modifier_combo_prefix() -> str:
    """Ctrl / Alt / Win / Shift (Shift only with another mod)."""
    parts: list[str] = []
    if _key_down(0x11) or _key_down(0xA2) or _key_down(0xA3):
        parts.append("Ctrl")
    if _key_down(0x12) or _key_down(0xA4) or _key_down(0xA5):
        parts.append("Alt")
    if _key_down(0x5B) or _key_down(0x5C):
        parts.append("Win")
    if _shift_down() and parts:
        parts.append("Shift")
    return "+".join(parts)


def vk_to_key_name(vk_code: int) -> str | None:
    """Stable key name for combos (not shifted glyph)."""
    if vk_code in _MODIFIER_VKS:
        return None
    if vk_code in _SPECIAL_LABELS:
        return _SPECIAL_LABELS[vk_code]
    if vk_code in _OEM_UNSHIFTED:
        return _OEM_UNSHIFTED[vk_code]
    if 0x70 <= vk_code <= 0x7B:
        return f"F{vk_code - 0x6F}"
    if 0x30 <= vk_code <= 0x39 or 0x41 <= vk_code <= 0x5A:
        return chr(vk_code)
    if 0x60 <= vk_code <= 0x69:
        return f"Num{vk_code - 0x60}"
    return None


def _typed_glyph(vk_code: int) -> str | None:
    """Character that would appear when typing this key (respects Shift / Caps)."""
    # Named action keys stay as labels (Bksp, Tab, …).
    if vk_code in _SPECIAL_LABELS:
        return _SPECIAL_LABELS[vk_code]

    buf = ctypes.create_unicode_buffer(8)
    state = (ctypes.c_byte * 256)()
    user32.GetKeyboardState(state)
    # LL hooks often see stale GetKeyboardState — force Shift from AsyncKeyState.
    if _shift_down():
        state[0x10] = ctypes.c_byte(0x80 | (int(state[0x10]) & 0x01))
        state[0xA0] = ctypes.c_byte(0x80)
        state[0xA1] = ctypes.c_byte(0x80)
    else:
        state[0x10] = ctypes.c_byte(int(state[0x10]) & 0x01)
        state[0xA0] = ctypes.c_byte(0)
        state[0xA1] = ctypes.c_byte(0)

    scan = user32.MapVirtualKeyW(vk_code, 0)
    rc = user32.ToUnicode(vk_code, scan, state, buf, len(buf), 0)
    # Drain dead-key state (accents) so the next call isn't sticky.
    if rc < 0:
        user32.ToUnicode(vk_code, scan, state, buf, len(buf), 0)
        return None
    if rc > 0:
        ch = buf.value[:rc].replace("\x00", "")
        if ch and all(c.isprintable() for c in ch):
            return ch[:3] if len(ch) > 1 else ch

    if _shift_down() and vk_code in _US_SHIFT_GLYPHS:
        return _US_SHIFT_GLYPHS[vk_code]
    if vk_code in _OEM_UNSHIFTED:
        return _OEM_UNSHIFTED[vk_code]
    if 0x41 <= vk_code <= 0x5A:
        caps = bool(user32.GetKeyState(0x14) & 0x0001)
        upper = caps ^ _shift_down()
        return chr(vk_code) if upper else chr(vk_code).lower()
    if 0x30 <= vk_code <= 0x39:
        return chr(vk_code)
    return None


def vk_to_label(vk_code: int, *, allow_combo: bool = True) -> str | None:
    """
    Badge text for a key press.
    With Ctrl/Alt/Win held → 'Ctrl+C' style combo.
    Otherwise the typed glyph (Shift+. → '>', Shift+1 → '!', a/A, …).
    """
    if vk_code in _MODIFIER_VKS:
        return None

    key_name = vk_to_key_name(vk_code)
    mods = modifier_combo_prefix() if allow_combo else ""
    if mods and key_name:
        return f"{mods}+{key_name}"

    glyph = _typed_glyph(vk_code)
    if glyph:
        return glyph
    if key_name:
        return key_name if len(key_name) <= 6 else key_name[:6]
    return "•"


@dataclass
class AnimState:
    # Click scale (spring-damper with overshoot)
    scale: float = 1.0
    scale_vel: float = 0.0
    pressed: bool = False
    press_scale: float = 0.88
    scale_spring: float = 260.0
    scale_damping: float = 16.0

    # Extra rotation from click / scroll / typing
    rot: float = 0.0
    rot_vel: float = 0.0
    rot_spring: float = 160.0
    rot_damping: float = 11.0
    click_rot_kick: float = -28.0
    scroll_rot_kick: float = 22.0
    scroll_inertia_boost: float = 380.0

    # Typing pop + key badge
    typing_enabled: bool = True
    type_bounce: float = 2.8
    type_rot_kick: float = 10.0
    type_alt: float = 1.0
    last_type_at: float = 0.0
    last_type_vk: int = 0
    badge_label: str = ""
    badge_alpha: float = 0.0
    badge_fade_s: float = 0.85
    badge_count: int = 1
    show_combos: bool = True

    # Speed → size (from inertia velocity). Positive = bigger when fast.
    speed_scale_amount: float = 0.18
    speed_scale_ref: float = 1400.0


def on_click_down(state: AnimState) -> None:
    state.pressed = True
    state.scale_vel -= 3.5
    state.rot_vel += state.click_rot_kick * 0.35
    state.rot += state.click_rot_kick * 0.25


def on_click_up(state: AnimState) -> None:
    state.pressed = False
    state.scale_vel += 2.5
    state.rot_vel -= state.click_rot_kick * 0.2


def on_scroll(state: AnimState, physics: InertiaState, wheel_delta: int) -> None:
    """Scroll nudges rotation (no full flips)."""
    if wheel_delta == 0:
        return
    direction = 1.0 if wheel_delta > 0 else -1.0
    notches = max(1, abs(wheel_delta) // 120)
    kick = direction * state.scroll_rot_kick * notches
    state.rot_vel += kick * 1.2
    state.rot += kick * 0.35
    physics.angular_vel += direction * state.scroll_inertia_boost * notches * 0.02
    physics.angle_deg += kick * 0.15


def on_key_type(state: AnimState, vk_code: int) -> None:
    """Bounce / wobble + show a key / shortcut badge on the cursor."""
    if not state.typing_enabled:
        return
    if vk_code in _MODIFIER_VKS:
        return
    now = time.perf_counter()
    if vk_code == state.last_type_vk and (now - state.last_type_at) < 0.045:
        return
    if (now - state.last_type_at) < 0.028:
        return
    state.last_type_at = now
    state.last_type_vk = int(vk_code)
    state.type_alt *= -1.0
    state.scale_vel += state.type_bounce
    state.rot_vel += state.type_alt * state.type_rot_kick
    state.rot += state.type_alt * state.type_rot_kick * 0.12

    label = vk_to_label(vk_code, allow_combo=state.show_combos)
    if label:
        if label == state.badge_label and state.badge_alpha > 0.05:
            state.badge_count = min(999, state.badge_count + 1)
        else:
            state.badge_count = 1
        state.badge_label = label
        state.badge_alpha = 1.0


def update_anim(state: AnimState, dt: float, *, speed: float = 0.0) -> tuple[float, float]:
    """Returns (scale, extra_rotation_deg)."""
    if dt <= 0:
        return state.scale, state.rot
    dt = min(dt, 0.05)

    if state.badge_alpha > 0.0:
        fade = max(0.15, state.badge_fade_s)
        state.badge_alpha = max(0.0, state.badge_alpha - dt / fade)
        if state.badge_alpha <= 0.0:
            state.badge_label = ""
            state.badge_count = 1

    speed_t = 0.0
    if state.speed_scale_ref > 1.0:
        speed_t = max(0.0, min(1.0, speed / state.speed_scale_ref))
    speed_mul = 1.0 + state.speed_scale_amount * speed_t

    base_target = state.press_scale if state.pressed else 1.0
    target_scale = base_target * speed_mul
    target_scale = max(0.55, min(1.55, target_scale))

    scale_accel = (
        state.scale_spring * (target_scale - state.scale)
        - state.scale_damping * state.scale_vel
    )
    state.scale_vel += scale_accel * dt
    state.scale += state.scale_vel * dt
    state.scale = max(0.55, min(1.55, state.scale))

    rot_accel = (-state.rot_spring * state.rot) - state.rot_damping * state.rot_vel
    state.rot_vel += rot_accel * dt
    state.rot += state.rot_vel * dt
    state.rot = max(-55.0, min(55.0, state.rot))

    return state.scale, state.rot
