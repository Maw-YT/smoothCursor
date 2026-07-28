"""Persistent animation / physics settings."""

from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


def settings_path() -> Path:
    """Prefer a writable AppData folder; fall back next to the executable."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        folder = Path(base) / "SmoothCursor"
    else:
        folder = Path.home() / ".config" / "smoothcursor"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "settings.json"


@dataclass
class Settings:
    # Master switches
    enabled: bool = True
    anim_enabled: bool = True
    inertia_enabled: bool = True
    typing_enabled: bool = True

    # Inertia (movement tilt)
    velocity_to_angle: float = 0.05
    max_angle: float = 40.0
    spring: float = 85.0
    damping: float = 12.0
    impulse: float = 0.014

    # Speed → cursor size (positive = bigger when fast, negative = smaller)
    speed_scale_amount: float = 0.18
    speed_scale_ref: float = 1400.0

    # Click scale spring
    press_scale: float = 0.88
    scale_spring: float = 260.0
    scale_damping: float = 16.0

    # Click / scroll rotation
    rot_spring: float = 160.0
    rot_damping: float = 11.0
    click_rot_kick: float = -28.0
    scroll_rot_kick: float = 22.0
    scroll_inertia_boost: float = 380.0

    # Typing
    type_bounce: float = 2.8
    type_rot_kick: float = 10.0
    badge_fade_s: float = 0.85
    show_combos: bool = True
    badge_style: str = "Pill"
    badge_theme: str = "Teal"
    badge_font: str = "Segoe UI"
    badge_size: float = 1.0

    # Animated cursor (.ani)
    ani_enabled: bool = False
    ani_path: str = ""
    ani_speed: float = 1.0
    ani_replace_all: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Settings:
        base = cls()
        if not data:
            return base
        known = {f.name for f in fields(cls)}
        for key, value in data.items():
            if key not in known:
                continue
            try:
                current = getattr(base, key)
                if isinstance(current, bool):
                    setattr(base, key, bool(value))
                else:
                    setattr(base, key, type(current)(value))
            except (TypeError, ValueError):
                continue
        if base.badge_style not in ("Dot", "Pill", "Square", "Soft"):
            base.badge_style = "Pill"
        if base.badge_theme not in (
            "Teal",
            "Coral",
            "Amber",
            "Sky",
            "Ink",
            "Ghost",
        ):
            base.badge_theme = "Teal"
        from .build import BADGE_FONTS

        if base.badge_font not in BADGE_FONTS:
            base.badge_font = "Segoe UI"
        return base

    def copy(self) -> Settings:
        return deepcopy(self)

    def apply_to_physics(self, physics) -> None:
        physics.velocity_to_angle = self.velocity_to_angle
        physics.max_angle = self.max_angle
        physics.spring = self.spring
        physics.damping = self.damping
        physics.impulse = self.impulse

    def apply_to_anim(self, anim) -> None:
        anim.press_scale = self.press_scale
        anim.scale_spring = self.scale_spring
        anim.scale_damping = self.scale_damping
        anim.rot_spring = self.rot_spring
        anim.rot_damping = self.rot_damping
        anim.click_rot_kick = self.click_rot_kick
        anim.scroll_rot_kick = self.scroll_rot_kick
        anim.scroll_inertia_boost = self.scroll_inertia_boost
        anim.typing_enabled = self.typing_enabled
        anim.type_bounce = self.type_bounce
        anim.type_rot_kick = self.type_rot_kick
        anim.badge_fade_s = self.badge_fade_s
        anim.show_combos = self.show_combos
        anim.speed_scale_amount = self.speed_scale_amount
        anim.speed_scale_ref = self.speed_scale_ref


# UI metadata: (label, min, max, step, section)
SETTING_META: dict[str, tuple[str, float, float, float, str]] = {
    "velocity_to_angle": ("Tilt sensitivity", 0.0, 0.2, 0.005, "Inertia"),
    "max_angle": ("Max tilt (°)", 5.0, 90.0, 1.0, "Inertia"),
    "spring": ("Inertia spring", 10.0, 300.0, 5.0, "Inertia"),
    "damping": ("Inertia damping", 1.0, 40.0, 0.5, "Inertia"),
    "impulse": ("Acceleration impulse", 0.0, 0.05, 0.001, "Inertia"),
    "speed_scale_amount": ("Speed size (+ bigger / − smaller)", -0.45, 0.45, 0.01, "Inertia"),
    "speed_scale_ref": ("Speed size at (px/s)", 400.0, 3000.0, 50.0, "Inertia"),
    "press_scale": ("Click shrink", 0.5, 1.0, 0.01, "Click"),
    "scale_spring": ("Scale spring", 50.0, 600.0, 10.0, "Click"),
    "scale_damping": ("Scale damping", 2.0, 40.0, 0.5, "Click"),
    "click_rot_kick": ("Click twist (°)", -90.0, 90.0, 1.0, "Click"),
    "rot_spring": ("Twist spring", 40.0, 400.0, 5.0, "Rotation"),
    "rot_damping": ("Twist damping", 2.0, 40.0, 0.5, "Rotation"),
    "scroll_rot_kick": ("Scroll twist (°)", 0.0, 90.0, 1.0, "Scroll"),
    "scroll_inertia_boost": ("Scroll spin boost", 0.0, 1000.0, 10.0, "Scroll"),
    "type_bounce": ("Key bounce", 0.0, 8.0, 0.1, "Typing"),
    "type_rot_kick": ("Key wobble (°)", 0.0, 40.0, 1.0, "Typing"),
    "badge_fade_s": ("Badge fade (s)", 0.2, 2.0, 0.05, "Typing"),
    "badge_size": ("Badge size", 0.7, 1.5, 0.05, "Typing"),
    "ani_speed": ("Playback speed", 0.25, 3.0, 0.05, "Ani"),
}


def load_settings(path: Path | None = None) -> Settings:
    path = path or settings_path()
    try:
        if path.is_file():
            with path.open("r", encoding="utf-8") as f:
                return Settings.from_dict(json.load(f))
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return Settings()


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(settings.to_dict(), f, indent=2)
        f.write("\n")
    tmp.replace(path)
    return path
