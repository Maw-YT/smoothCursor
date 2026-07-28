"""Click-through layered overlay for cursor trails and dust particles."""

from __future__ import annotations

import ctypes
import math
import random
from dataclasses import dataclass, field

import win32gui
from PIL import Image, ImageDraw

from .winapi import (
    AC_SRC_ALPHA,
    AC_SRC_OVER,
    BLENDFUNCTION,
    HWND_TOPMOST,
    POINT,
    SIZE,
    SWP_NOACTIVATE,
    SWP_SHOWWINDOW,
    ULW_ALPHA,
    WS_EX_LAYERED,
    WS_EX_TOOLWINDOW,
    WS_EX_TOPMOST,
    WS_EX_TRANSPARENT,
    WS_POPUP,
    user32,
)

# Theme name → RGB palette (trail uses [0], dust samples the list)
EFFECT_THEMES: dict[str, tuple[tuple[int, int, int], ...]] = {
    "Teal": ((45, 212, 191), (94, 234, 212), (20, 184, 166)),
    "Coral": ((251, 113, 133), (244, 63, 94), (253, 164, 175)),
    "Amber": ((251, 191, 36), (245, 158, 11), (253, 224, 71)),
    "Sky": ((56, 189, 248), (14, 165, 233), (125, 211, 252)),
    "Soft": ((232, 236, 244), (203, 213, 225), (148, 163, 184)),
    "Ink": ((30, 41, 59), (71, 85, 105), (100, 116, 139)),
    "Mix": (
        (45, 212, 191),
        (232, 236, 244),
        (251, 191, 36),
        (148, 163, 184),
        (251, 113, 133),
    ),
}


@dataclass
class _TrailPoint:
    x: float
    y: float
    age: float = 0.0


@dataclass
class _Particle:
    x: float
    y: float
    vx: float
    vy: float
    life: float
    max_life: float
    size: float
    color: tuple[int, int, int]


@dataclass
class EffectsOverlay:
    """Topmost click-through window that paints trails + dust around the cursor."""

    size: int = 360
    trails_enabled: bool = True
    trail_length: float = 12.0
    trail_opacity: float = 0.55
    trail_size: float = 1.0
    trail_fade: float = 1.0
    trail_spacing: float = 8.0  # ms between samples
    trail_min_speed: float = 25.0
    trail_theme: str = "Teal"

    dust_enabled: bool = True
    dust_amount: float = 0.65
    dust_speed_ref: float = 900.0
    dust_min_speed: float = 0.35  # fraction of speed_ref before spawn
    dust_size: float = 1.0
    dust_life: float = 1.0
    dust_gravity: float = 180.0
    dust_spread: float = 1.0
    dust_drag: float = 0.96
    dust_opacity: float = 1.0
    dust_click_burst: float = 1.0
    dust_on_click: bool = True
    dust_theme: str = "Mix"

    _hwnd: int = 0
    _trail: list[_TrailPoint] = field(default_factory=list)
    _particles: list[_Particle] = field(default_factory=list)
    _sample_accum: float = 0.0
    _last_x: float | None = None
    _last_y: float | None = None
    _created: bool = False

    def create(self) -> None:
        if self._created:
            return
        ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
        self._hwnd = int(
            win32gui.CreateWindowEx(
                ex,
                "static",
                "SmoothCursorEffects",
                WS_POPUP,
                0,
                0,
                self.size,
                self.size,
                0,
                0,
                0,
                None,
            )
        )
        if not self._hwnd:
            return
        win32gui.SetWindowPos(
            self._hwnd,
            HWND_TOPMOST,
            0,
            0,
            self.size,
            self.size,
            SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
        self._created = True
        self._blit_clear()

    def destroy(self) -> None:
        self._trail.clear()
        self._particles.clear()
        if self._hwnd:
            try:
                win32gui.DestroyWindow(self._hwnd)
            except Exception:
                pass
        self._hwnd = 0
        self._created = False

    def apply_settings(self, settings) -> None:
        """Pull every trails/dust field from a Settings-like object."""
        self.trails_enabled = bool(getattr(settings, "trails_enabled", True))
        self.trail_length = max(2.0, min(48.0, float(getattr(settings, "trail_length", 12.0))))
        self.trail_opacity = max(0.05, min(1.0, float(getattr(settings, "trail_opacity", 0.55))))
        self.trail_size = max(0.25, min(3.0, float(getattr(settings, "trail_size", 1.0))))
        self.trail_fade = max(0.25, min(3.0, float(getattr(settings, "trail_fade", 1.0))))
        self.trail_spacing = max(2.0, min(40.0, float(getattr(settings, "trail_spacing", 8.0))))
        self.trail_min_speed = max(0.0, min(800.0, float(getattr(settings, "trail_min_speed", 25.0))))
        theme = str(getattr(settings, "trail_theme", "Teal"))
        self.trail_theme = theme if theme in EFFECT_THEMES else "Teal"

        self.dust_enabled = bool(getattr(settings, "dust_enabled", True))
        self.dust_amount = max(0.0, min(3.0, float(getattr(settings, "dust_amount", 0.65))))
        self.dust_speed_ref = max(200.0, float(getattr(settings, "dust_speed_ref", 900.0)))
        self.dust_min_speed = max(0.0, min(1.0, float(getattr(settings, "dust_min_speed", 0.35))))
        self.dust_size = max(0.25, min(3.0, float(getattr(settings, "dust_size", 1.0))))
        self.dust_life = max(0.25, min(3.0, float(getattr(settings, "dust_life", 1.0))))
        self.dust_gravity = max(-200.0, min(600.0, float(getattr(settings, "dust_gravity", 180.0))))
        self.dust_spread = max(0.1, min(3.0, float(getattr(settings, "dust_spread", 1.0))))
        self.dust_drag = max(0.85, min(0.995, float(getattr(settings, "dust_drag", 0.96))))
        self.dust_opacity = max(0.05, min(1.0, float(getattr(settings, "dust_opacity", 1.0))))
        self.dust_click_burst = max(0.0, min(3.0, float(getattr(settings, "dust_click_burst", 1.0))))
        self.dust_on_click = bool(getattr(settings, "dust_on_click", True))
        dtheme = str(getattr(settings, "dust_theme", "Mix"))
        self.dust_theme = dtheme if dtheme in EFFECT_THEMES else "Mix"

        if not self.trails_enabled:
            self._trail.clear()
        if not self.dust_enabled:
            self._particles.clear()

    def _trail_palette(self) -> tuple[tuple[int, int, int], ...]:
        return EFFECT_THEMES.get(self.trail_theme, EFFECT_THEMES["Teal"])

    def _dust_palette(self) -> tuple[tuple[int, int, int], ...]:
        return EFFECT_THEMES.get(self.dust_theme, EFFECT_THEMES["Mix"])

    def update(
        self,
        x: float,
        y: float,
        vx: float,
        vy: float,
        dt: float,
        *,
        clicked: bool = False,
    ) -> None:
        if not self._created or not self._hwnd:
            return
        dt = max(0.0, min(0.05, float(dt)))
        speed = math.hypot(vx, vy)

        if self.trails_enabled:
            self._sample_accum += dt
            interval = self.trail_spacing / 1000.0
            if speed > self.trail_min_speed and self._sample_accum >= interval:
                self._sample_accum = 0.0
                self._trail.append(_TrailPoint(x, y))
            max_pts = int(self.trail_length)
            life = (0.08 + self.trail_length * 0.012) * self.trail_fade
            for p in self._trail:
                p.age += dt
            self._trail = [p for p in self._trail if p.age < life]
            if len(self._trail) > max_pts:
                self._trail = self._trail[-max_pts:]
        else:
            self._trail.clear()

        if self.dust_enabled:
            self._spawn_dust(x, y, vx, vy, speed, clicked)
            self._step_particles(dt)
        else:
            self._particles.clear()

        self._last_x, self._last_y = x, y
        if not self._trail and not self._particles:
            self._blit_clear()
            self._move_window(x, y)
            return
        self._render(x, y)

    def _spawn_dust(
        self,
        x: float,
        y: float,
        vx: float,
        vy: float,
        speed: float,
        clicked: bool,
    ) -> None:
        if len(self._particles) > 160:
            return
        palette = self._dust_palette()
        ref = max(1.0, self.dust_speed_ref)
        t = max(0.0, min(1.0, speed / ref))
        life_mul = self.dust_life
        size_mul = self.dust_size
        spread = self.dust_spread

        if clicked and self.dust_on_click and self.dust_click_burst > 0.01:
            n = max(2, int(6 * self.dust_amount * self.dust_click_burst))
            for _ in range(n):
                ang = random.uniform(0, math.tau)
                sp = random.uniform(80, 280) * (0.6 + 0.4 * spread)
                self._particles.append(
                    _Particle(
                        x=x + random.uniform(-4, 4) * spread,
                        y=y + random.uniform(-4, 4) * spread,
                        vx=math.cos(ang) * sp,
                        vy=math.sin(ang) * sp,
                        life=0.0,
                        max_life=random.uniform(0.25, 0.55) * life_mul,
                        size=random.uniform(1.5, 3.5) * size_mul,
                        color=random.choice(palette),
                    )
                )

        if t < self.dust_min_speed or self.dust_amount <= 0.01:
            return
        rate = t * self.dust_amount * 18.0
        expect = rate * 0.016
        if random.random() < min(0.9, expect):
            bx = x - (vx / max(speed, 1.0)) * 6.0
            by = y - (vy / max(speed, 1.0)) * 6.0
            perp_x = -vy / max(speed, 1.0)
            perp_y = vx / max(speed, 1.0)
            off = random.uniform(-8, 8) * spread
            self._particles.append(
                _Particle(
                    x=bx + perp_x * off,
                    y=by + perp_y * off,
                    vx=vx * 0.15 + perp_x * random.uniform(-40, 40) * spread,
                    vy=vy * 0.15 + perp_y * random.uniform(-40, 40) * spread + 20,
                    life=0.0,
                    max_life=random.uniform(0.2, 0.45) * life_mul,
                    size=random.uniform(1.2, 2.8) * size_mul,
                    color=random.choice(palette),
                )
            )

    def _step_particles(self, dt: float) -> None:
        alive: list[_Particle] = []
        drag = self.dust_drag
        grav = self.dust_gravity
        for p in self._particles:
            p.life += dt
            if p.life >= p.max_life:
                continue
            p.vy += grav * dt
            p.vx *= drag
            p.vy *= drag
            p.x += p.vx * dt
            p.y += p.vy * dt
            alive.append(p)
        self._particles = alive

    def _move_window(self, cx: float, cy: float) -> None:
        half = self.size // 2
        left = int(round(cx - half))
        top = int(round(cy - half))
        try:
            win32gui.SetWindowPos(
                self._hwnd,
                HWND_TOPMOST,
                left,
                top,
                self.size,
                self.size,
                SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
        except Exception:
            pass

    def _blit_clear(self) -> None:
        img = Image.new("RGBA", (self.size, self.size), (0, 0, 0, 0))
        self._update_layered(img)

    def _render(self, cx: float, cy: float) -> None:
        half = self.size / 2.0
        img = Image.new("RGBA", (self.size, self.size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        trail_rgb = self._trail_palette()[0]
        life = max(0.05, (0.08 + self.trail_length * 0.012) * self.trail_fade)

        if self._trail:
            n = len(self._trail)
            for i, p in enumerate(self._trail):
                lx = p.x - (cx - half)
                ly = p.y - (cy - half)
                fade = 1.0 - (p.age / life)
                fade = max(0.0, min(1.0, fade))
                fade *= (i + 1) / n
                alpha = int(255 * self.trail_opacity * fade * 0.7)
                if alpha < 8:
                    continue
                r = (2.0 + 4.0 * fade) * self.trail_size
                draw.ellipse(
                    (lx - r, ly - r, lx + r, ly + r),
                    fill=(*trail_rgb, alpha),
                )

        dust_a = self.dust_opacity
        for p in self._particles:
            t = 1.0 - p.life / max(0.01, p.max_life)
            alpha = int(255 * t * t * dust_a)
            if alpha < 10:
                continue
            lx = p.x - (cx - half)
            ly = p.y - (cy - half)
            r = p.size * (0.6 + 0.4 * t)
            draw.ellipse(
                (lx - r, ly - r, lx + r, ly + r),
                fill=(*p.color, alpha),
            )

        self._move_window(cx, cy)
        self._update_layered(img)

    def _update_layered(self, img: Image.Image) -> None:
        if not self._hwnd:
            return
        img = img.convert("RGBA")
        w, h = img.size
        raw = img.tobytes("raw", "BGRA")

        hdc_screen = win32gui.GetDC(0)
        hdc_mem = win32gui.CreateCompatibleDC(hdc_screen)
        try:
            from .winapi import (
                BI_BITFIELDS,
                BITMAPV5HEADER,
                DIB_RGB_COLORS,
                gdi32,
            )

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
            bits = ctypes.c_void_p()
            hbmp = gdi32.CreateDIBSection(
                hdc_mem, ctypes.byref(bi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
            )
            if not hbmp or not bits:
                return
            ctypes.memmove(bits, raw, len(raw))
            old = win32gui.SelectObject(hdc_mem, hbmp)

            blend = BLENDFUNCTION()
            blend.BlendOp = AC_SRC_OVER
            blend.BlendFlags = 0
            blend.SourceConstantAlpha = 255
            blend.AlphaFormat = AC_SRC_ALPHA

            size = SIZE(w, h)
            src = POINT(0, 0)
            user32.UpdateLayeredWindow(
                self._hwnd,
                hdc_screen,
                None,
                ctypes.byref(size),
                hdc_mem,
                ctypes.byref(src),
                0,
                ctypes.byref(blend),
                ULW_ALPHA,
            )
            win32gui.SelectObject(hdc_mem, old)
            win32gui.DeleteObject(hbmp)
        except Exception:
            pass
        finally:
            win32gui.DeleteDC(hdc_mem)
            win32gui.ReleaseDC(0, hdc_screen)
