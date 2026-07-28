"""Modern Smooth Cursor settings UI with system tray."""

from __future__ import annotations

import threading
from typing import Any

import customtkinter as ctk
from PIL import Image, ImageDraw
from tkinter import filedialog, messagebox

from .ani import try_load_ani
from .app import SmoothCursorApp, restore_cursor_visibility
from .build import BADGE_FONTS, BADGE_STYLES, BADGE_THEMES
from .settings import SETTING_META, Settings, load_settings, save_settings, settings_path
from .winapi import SPI_SETCURSORS, user32

# Charcoal + teal — avoid default purple/blue AI look
_COLORS = {
    "bg": "#0f1115",
    "panel": "#171a21",
    "card": "#1e232d",
    "border": "#2a3140",
    "text": "#e8ecf4",
    "muted": "#8b93a7",
    "accent": "#2dd4bf",
    "accent_hover": "#14b8a6",
    "danger": "#f87171",
    "ok": "#34d399",
}

_SECTION_ORDER = ("Inertia", "Click", "Rotation", "Scroll", "Typing", "Ani")

# Tab name → (blurb, enable_key|None, sections from SETTING_META)
_TABS: tuple[tuple[str, str, str | None, tuple[str, ...]], ...] = (
    (
        "General",
        "Turn features on or off. Fine-tune each one in its own tab.",
        None,
        (),
    ),
    (
        "Inertia",
        "Tilt from movement, plus grow/shrink size based on speed.",
        "inertia_enabled",
        ("Inertia",),
    ),
    (
        "Click",
        "Scale and twist feedback when you press and release.",
        "anim_enabled",
        ("Click",),
    ),
    (
        "Scroll",
        "Wheel notches nudge rotation and a bit of spin.",
        "anim_enabled",
        ("Scroll",),
    ),
    (
        "Typing",
        "Key / shortcut badges on the cursor (Ctrl+C, Win+E, …).",
        "typing_enabled",
        ("Typing",),
    ),
    (
        "Ani",
        "Optional .ani override. Scheme Wait/Busy .ani files animate automatically.",
        "ani_enabled",
        ("Ani",),
    ),
    (
        "Twist",
        "How quickly click / scroll / typing twist springs back.",
        "anim_enabled",
        ("Rotation",),
    ),
)


def _tray_icon_image() -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Soft disc
    d.ellipse((4, 4, 60, 60), fill=(30, 35, 45, 255))
    # Pointer wedge
    d.polygon([(18, 14), (18, 48), (28, 40), (36, 54), (42, 50), (34, 36), (46, 36)], fill=(45, 212, 191, 255))
    return img


class SmoothCursorGUI:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.app: SmoothCursorApp | None = None
        self._thread: threading.Thread | None = None
        self._tray = None
        self._tray_thread: threading.Thread | None = None
        self._quitting = False
        self._vars: dict[str, ctk.BooleanVar | ctk.DoubleVar] = {}
        self._value_labels: dict[str, ctk.CTkLabel] = {}
        self._applying = False

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.root = ctk.CTk()
        self.root.title("Smooth Cursor")
        self.root.geometry("520x740")
        self.root.minsize(480, 640)
        self.root.configure(fg_color=_COLORS["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        # Start hidden; tray icon is the primary surface.
        self.root.withdraw()

        self._status_text = ctk.StringVar(master=self.root, value="Starting…")
        self._build()
        self._load_into_ui(self.settings)
        self._setup_tray()
        self.root.after(80, self._boot)
        self.root.after(400, self._poll_status)

    def _boot(self) -> None:
        self._start_engine()
        # Stay in tray; only surface the window if tray failed.
        if self._tray is None:
            self._show_window()

    def _build(self) -> None:
        shell = ctk.CTkFrame(self.root, fg_color="transparent")
        shell.pack(fill="both", expand=True, padx=20, pady=18)

        # Header
        header = ctk.CTkFrame(shell, fg_color="transparent")
        header.pack(fill="x", pady=(0, 14))
        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            titles,
            text="Smooth Cursor",
            font=ctk.CTkFont(family="Segoe UI Semibold", size=22),
            text_color=_COLORS["text"],
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            titles,
            text="Feature settings",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=_COLORS["muted"],
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        self._status_pill = ctk.CTkLabel(
            header,
            textvariable=self._status_text,
            font=ctk.CTkFont(family="Segoe UI Semibold", size=12),
            text_color=_COLORS["bg"],
            fg_color=_COLORS["accent"],
            corner_radius=999,
            padx=12,
            pady=6,
        )
        self._status_pill.pack(side="right")

        self._vars["inertia_enabled"] = ctk.BooleanVar(value=True)
        self._vars["anim_enabled"] = ctk.BooleanVar(value=True)
        self._vars["typing_enabled"] = ctk.BooleanVar(value=True)
        self._vars["show_combos"] = ctk.BooleanVar(value=True)
        self._vars["ani_enabled"] = ctk.BooleanVar(value=False)
        self._vars["ani_replace_all"] = ctk.BooleanVar(value=False)
        self._ani_path = ctk.StringVar(value="")
        self._ani_status = ctk.StringVar(value="No .ani loaded")
        self._choice_vars: dict[str, ctk.StringVar] = {
            "badge_style": ctk.StringVar(value="Pill"),
            "badge_theme": ctk.StringVar(value="Teal"),
            "badge_font": ctk.StringVar(value="Segoe UI"),
        }

        buckets: dict[str, list[tuple[str, str, float, float, float]]] = {s: [] for s in _SECTION_ORDER}
        for key, (label, lo, hi, step, section) in SETTING_META.items():
            buckets.setdefault(section, []).append((key, label, lo, hi, step))

        tabs = ctk.CTkTabview(
            shell,
            fg_color=_COLORS["panel"],
            segmented_button_fg_color=_COLORS["card"],
            segmented_button_selected_color=_COLORS["accent"],
            segmented_button_selected_hover_color=_COLORS["accent_hover"],
            segmented_button_unselected_color=_COLORS["card"],
            segmented_button_unselected_hover_color=_COLORS["border"],
            text_color=_COLORS["bg"],
            text_color_disabled=_COLORS["muted"],
            corner_radius=14,
            border_width=1,
            border_color=_COLORS["border"],
            anchor="nw",
        )
        tabs.pack(fill="both", expand=True)
        # Unselected tab labels need readable contrast on dark cards
        try:
            tabs._segmented_button.configure(text_color=_COLORS["text"])  # noqa: SLF001
        except Exception:
            pass

        for tab_name, blurb, enable_key, sections in _TABS:
            frame = tabs.add(tab_name)
            frame.configure(fg_color="transparent")
            scroll = ctk.CTkScrollableFrame(
                frame,
                fg_color="transparent",
                scrollbar_button_color=_COLORS["border"],
                scrollbar_button_hover_color=_COLORS["accent"],
            )
            scroll.pack(fill="both", expand=True, padx=4, pady=8)

            ctk.CTkLabel(
                scroll,
                text=blurb,
                font=ctk.CTkFont(family="Segoe UI", size=12),
                text_color=_COLORS["muted"],
                anchor="w",
                justify="left",
                wraplength=400,
            ).pack(fill="x", padx=8, pady=(4, 12))

            if tab_name == "General":
                card = self._feature_card(scroll)
                card.pack(fill="x", padx=4, pady=(0, 8))
                body = self._card_body(card)
                switches = (
                    ("inertia_enabled", "Movement inertia", "Tilt + size change from speed"),
                    ("anim_enabled", "Click & scroll anim", "Scale and twist on click / wheel"),
                    ("typing_enabled", "Typing badge", "Show keys on the cursor as you type"),
                    ("ani_enabled", "Custom .ani override", "Optional — scheme Wait/Busy already animate"),
                )
                for i, (key, title, subtitle) in enumerate(switches):
                    if i:
                        ctk.CTkFrame(body, height=1, fg_color=_COLORS["border"]).pack(
                            fill="x", pady=10
                        )
                    self._add_switch(body, title, subtitle, self._vars[key])
                tip = self._feature_card(scroll)
                tip.pack(fill="x", padx=4, pady=(8, 4))
                tip_body = self._card_body(tip)
                ctk.CTkLabel(
                    tip_body,
                    text="Tip",
                    font=ctk.CTkFont(family="Segoe UI Semibold", size=12),
                    text_color=_COLORS["accent"],
                    anchor="w",
                ).pack(anchor="w")
                ctk.CTkLabel(
                    tip_body,
                    text="Each tab tunes one feature. Closing this window hides to the tray.",
                    font=ctk.CTkFont(family="Segoe UI", size=12),
                    text_color=_COLORS["muted"],
                    anchor="w",
                    justify="left",
                    wraplength=400,
                ).pack(fill="x", pady=(4, 0))
                continue

            if enable_key is not None:
                enable_card = self._feature_card(scroll)
                enable_card.pack(fill="x", padx=4, pady=(0, 10))
                enable_body = self._card_body(enable_card)
                titles = {
                    "inertia_enabled": ("Enable inertia", "Tilt and speed-based size"),
                    "anim_enabled": ("Enable click / scroll", "Shared by Click, Scroll, and Twist"),
                    "typing_enabled": ("Enable typing", "Key badge + bounce on keystrokes"),
                    "ani_enabled": ("Enable custom .ani", "Optional override — Windows scheme .ani already play"),
                }
                title, subtitle = titles.get(enable_key, ("Enable", ""))
                self._add_switch(enable_body, title, subtitle, self._vars[enable_key])

            if tab_name == "Ani":
                file_card = self._feature_card(scroll)
                file_card.pack(fill="x", padx=4, pady=(0, 10))
                file_body = self._card_body(file_card)
                ctk.CTkLabel(
                    file_body,
                    text="ANI FILE",
                    font=ctk.CTkFont(family="Segoe UI Semibold", size=11),
                    text_color=_COLORS["accent"],
                    anchor="w",
                ).pack(fill="x", pady=(0, 8))
                ctk.CTkLabel(
                    file_body,
                    text="Windows scheme Wait / Busy .ani animate automatically. This picks an optional custom override.",
                    font=ctk.CTkFont(family="Segoe UI", size=11),
                    text_color=_COLORS["muted"],
                    anchor="w",
                    justify="left",
                    wraplength=400,
                ).pack(fill="x", pady=(0, 8))
                ctk.CTkLabel(
                    file_body,
                    textvariable=self._ani_path,
                    font=ctk.CTkFont(family="Segoe UI", size=11),
                    text_color=_COLORS["muted"],
                    anchor="w",
                    wraplength=380,
                    justify="left",
                ).pack(fill="x", pady=(0, 6))
                ctk.CTkLabel(
                    file_body,
                    textvariable=self._ani_status,
                    font=ctk.CTkFont(family="Segoe UI", size=11),
                    text_color=_COLORS["text"],
                    anchor="w",
                ).pack(fill="x", pady=(0, 10))
                row = ctk.CTkFrame(file_body, fg_color="transparent")
                row.pack(fill="x")
                ctk.CTkButton(
                    row,
                    text="Browse…",
                    command=self._browse_ani,
                    fg_color=_COLORS["accent"],
                    hover_color=_COLORS["accent_hover"],
                    text_color=_COLORS["bg"],
                    font=ctk.CTkFont(family="Segoe UI Semibold", size=12),
                    height=34,
                    corner_radius=8,
                    width=100,
                ).pack(side="left")
                ctk.CTkButton(
                    row,
                    text="Clear",
                    command=self._clear_ani,
                    fg_color=_COLORS["panel"],
                    hover_color=_COLORS["border"],
                    border_width=1,
                    border_color=_COLORS["border"],
                    text_color=_COLORS["muted"],
                    font=ctk.CTkFont(family="Segoe UI", size=12),
                    height=34,
                    corner_radius=8,
                    width=80,
                ).pack(side="left", padx=(8, 0))
                ctk.CTkFrame(file_body, height=1, fg_color=_COLORS["border"]).pack(
                    fill="x", pady=12
                )
                self._add_switch(
                    file_body,
                    "Replace all system cursors",
                    "Otherwise only the normal arrow is animated",
                    self._vars["ani_replace_all"],
                )

            if tab_name == "Typing":
                art = self._feature_card(scroll)
                art.pack(fill="x", padx=4, pady=(0, 10))
                art_body = self._card_body(art)
                ctk.CTkLabel(
                    art_body,
                    text="BADGE ART",
                    font=ctk.CTkFont(family="Segoe UI Semibold", size=11),
                    text_color=_COLORS["accent"],
                    anchor="w",
                ).pack(fill="x", pady=(0, 8))
                self._add_switch(
                    art_body,
                    "Shortcut combos",
                    "Show Ctrl+C / Alt+Tab / Win+E style labels",
                    self._vars["show_combos"],
                )
                ctk.CTkFrame(art_body, height=1, fg_color=_COLORS["border"]).pack(
                    fill="x", pady=12
                )
                self._add_choice(
                    art_body,
                    "Shape",
                    self._choice_vars["badge_style"],
                    list(BADGE_STYLES),
                )
                self._add_choice(
                    art_body,
                    "Color theme",
                    self._choice_vars["badge_theme"],
                    list(BADGE_THEMES.keys()),
                )
                self._add_choice(
                    art_body,
                    "Font",
                    self._choice_vars["badge_font"],
                    list(BADGE_FONTS.keys()),
                )

            for section in sections:
                items = buckets.get(section) or []
                if not items:
                    continue
                card = self._feature_card(scroll)
                card.pack(fill="x", padx=4, pady=(0, 10))
                body = self._card_body(card)
                ctk.CTkLabel(
                    body,
                    text=section.upper(),
                    font=ctk.CTkFont(family="Segoe UI Semibold", size=11),
                    text_color=_COLORS["accent"],
                    anchor="w",
                ).pack(fill="x", pady=(0, 6))
                for key, label, lo, hi, step in items:
                    self._add_slider(body, key, label, lo, hi, step)

        # Actions
        actions = ctk.CTkFrame(shell, fg_color="transparent")
        actions.pack(fill="x", pady=(12, 4))

        self._engine_btn = ctk.CTkButton(
            actions,
            text="Stop",
            command=self._toggle_engine,
            fg_color=_COLORS["accent"],
            hover_color=_COLORS["accent_hover"],
            text_color=_COLORS["bg"],
            font=ctk.CTkFont(family="Segoe UI Semibold", size=13),
            height=40,
            corner_radius=10,
        )
        self._engine_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))

        ctk.CTkButton(
            actions,
            text="Save",
            command=self._save,
            fg_color=_COLORS["panel"],
            hover_color=_COLORS["border"],
            border_width=1,
            border_color=_COLORS["border"],
            text_color=_COLORS["text"],
            font=ctk.CTkFont(family="Segoe UI Semibold", size=13),
            height=40,
            corner_radius=10,
            width=88,
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            actions,
            text="Reset",
            command=self._reset,
            fg_color=_COLORS["panel"],
            hover_color=_COLORS["border"],
            border_width=1,
            border_color=_COLORS["border"],
            text_color=_COLORS["muted"],
            font=ctk.CTkFont(family="Segoe UI", size=13),
            height=40,
            corner_radius=10,
            width=88,
        ).pack(side="left", padx=(4, 0))

        foot = ctk.CTkFrame(shell, fg_color="transparent")
        foot.pack(fill="x", pady=(10, 0))
        ctk.CTkButton(
            foot,
            text="Hide to tray",
            command=self._hide_to_tray,
            fg_color="transparent",
            hover_color=_COLORS["panel"],
            text_color=_COLORS["muted"],
            font=ctk.CTkFont(family="Segoe UI", size=12),
            height=32,
        ).pack(side="left")
        ctk.CTkLabel(
            foot,
            text=str(settings_path().name),
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=_COLORS["muted"],
        ).pack(side="right")

    def _feature_card(self, parent: ctk.CTkBaseClass) -> ctk.CTkFrame:
        return ctk.CTkFrame(
            parent,
            fg_color=_COLORS["card"],
            corner_radius=14,
            border_width=1,
            border_color=_COLORS["border"],
        )

    def _card_body(self, card: ctk.CTkFrame) -> ctk.CTkFrame:
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=16, pady=14)
        return body

    def _add_choice(
        self,
        parent: ctk.CTkFrame,
        label: str,
        var: ctk.StringVar,
        values: list[str],
    ) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(
            row,
            text=label,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=_COLORS["text"],
            anchor="w",
            width=100,
        ).pack(side="left")
        menu = ctk.CTkOptionMenu(
            row,
            variable=var,
            values=values,
            command=lambda _v: self._live_apply(),
            fg_color=_COLORS["panel"],
            button_color=_COLORS["border"],
            button_hover_color=_COLORS["accent"],
            dropdown_fg_color=_COLORS["card"],
            dropdown_hover_color=_COLORS["border"],
            text_color=_COLORS["text"],
            font=ctk.CTkFont(family="Segoe UI", size=12),
            height=32,
            corner_radius=8,
        )
        menu.pack(side="right", fill="x", expand=True, padx=(8, 0))

    def _add_switch(
        self, parent: ctk.CTkFrame, title: str, subtitle: str, var: ctk.BooleanVar
    ) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x")
        text = ctk.CTkFrame(row, fg_color="transparent")
        text.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            text,
            text=title,
            font=ctk.CTkFont(family="Segoe UI Semibold", size=13),
            text_color=_COLORS["text"],
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            text,
            text=subtitle,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=_COLORS["muted"],
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkSwitch(
            row,
            text="",
            variable=var,
            command=self._on_toggle,
            progress_color=_COLORS["accent"],
            button_color=_COLORS["text"],
            button_hover_color=_COLORS["text"],
            fg_color=_COLORS["border"],
            width=46,
        ).pack(side="right")

    def _add_slider(
        self,
        parent: ctk.CTkFrame,
        key: str,
        label: str,
        lo: float,
        hi: float,
        step: float,
    ) -> None:
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.pack(fill="x", pady=(4, 8))
        top = ctk.CTkFrame(wrap, fg_color="transparent")
        top.pack(fill="x")
        ctk.CTkLabel(
            top,
            text=label,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=_COLORS["text"],
            anchor="w",
        ).pack(side="left")
        value_lbl = ctk.CTkLabel(
            top,
            text="",
            font=ctk.CTkFont(family="Segoe UI Semibold", size=12),
            text_color=_COLORS["accent"],
            anchor="e",
        )
        value_lbl.pack(side="right")
        self._value_labels[key] = value_lbl

        var = ctk.DoubleVar(value=float(getattr(Settings(), key)))
        self._vars[key] = var

        def _on_slide(value: Any) -> None:
            if self._applying:
                return
            self._fmt_value(key, float(value), step, lo, hi)
            self._live_apply()

        ctk.CTkSlider(
            wrap,
            from_=lo,
            to=hi,
            number_of_steps=max(1, int(round((hi - lo) / max(step, 1e-6)))),
            variable=var,
            command=_on_slide,
            progress_color=_COLORS["accent"],
            button_color=_COLORS["text"],
            button_hover_color=_COLORS["accent"],
            fg_color=_COLORS["border"],
            height=16,
        ).pack(fill="x", pady=(6, 0))
        self._fmt_value(key, float(var.get()), step, lo, hi)

    def _fmt_value(self, key: str, v: float, step: float, lo: float, hi: float) -> None:
        lbl = self._value_labels.get(key)
        if lbl is None:
            return
        if abs(step - int(step)) < 1e-9 and abs(hi - lo) >= 10:
            text = f"{v:.0f}"
        elif step >= 1:
            text = f"{v:.1f}"
        else:
            text = f"{v:.3f}".rstrip("0").rstrip(".") or "0"
        lbl.configure(text=text)

    def _browse_ani(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Select animated cursor",
            filetypes=[("Animated cursor", "*.ani"), ("All files", "*.*")],
        )
        if not path:
            return
        ani = try_load_ani(path)
        if ani is None:
            messagebox.showerror("Smooth Cursor", "Could not load that .ani file.")
            return
        self._ani_path.set(path)
        self._ani_status.set(f"Loaded · {ani.title} · {len(ani.frames)} frames · {len(ani.steps)} steps")
        self._vars["ani_enabled"].set(True)
        self._live_apply()

    def _clear_ani(self) -> None:
        self._ani_path.set("")
        self._ani_status.set("No .ani loaded")
        self._vars["ani_enabled"].set(False)
        self._live_apply()

    def _refresh_ani_status(self) -> None:
        path = self._ani_path.get().strip()
        if not path:
            self._ani_status.set("No .ani loaded")
            return
        ani = try_load_ani(path)
        if ani is None:
            self._ani_status.set("Invalid or missing .ani file")
            return
        self._ani_status.set(
            f"Loaded · {ani.title} · {len(ani.frames)} frames · {len(ani.steps)} steps"
        )

    def _collect(self) -> Settings:
        data = self.settings.to_dict()
        for key, var in self._vars.items():
            data[key] = var.get()
        for key, var in self._choice_vars.items():
            data[key] = var.get()
        data["ani_path"] = self._ani_path.get().strip()
        data["enabled"] = True
        return Settings.from_dict(data)

    def _load_into_ui(self, settings: Settings) -> None:
        self._applying = True
        try:
            for key, var in self._vars.items():
                if hasattr(settings, key):
                    var.set(getattr(settings, key))
            for key, var in self._choice_vars.items():
                if hasattr(settings, key):
                    var.set(str(getattr(settings, key)))
            self._ani_path.set(settings.ani_path or "")
            for key, (label, lo, hi, step, _section) in SETTING_META.items():
                if key in self._vars:
                    self._fmt_value(key, float(self._vars[key].get()), step, lo, hi)
            self._refresh_ani_status()
        finally:
            self._applying = False

    def _on_toggle(self) -> None:
        self._live_apply()

    def _live_apply(self) -> None:
        settings = self._collect()
        self.settings = settings
        if self.app is not None and self.app.is_running:
            self.app.apply_settings(settings)

    def _save(self) -> None:
        self.settings = self._collect()
        path = save_settings(self.settings)
        if self.app is not None and self.app.is_running:
            self.app.apply_settings(self.settings)
        self._flash_status(f"Saved · {path.name}", ok=True)

    def _reset(self) -> None:
        if not messagebox.askyesno("Reset", "Restore default animation settings?"):
            return
        self.settings = Settings()
        self._load_into_ui(self.settings)
        self._live_apply()
        self._flash_status("Defaults restored", ok=True)

    def _flash_status(self, text: str, *, ok: bool = False) -> None:
        self._status_text.set(text)
        self._status_pill.configure(fg_color=_COLORS["ok"] if ok else _COLORS["accent"])
        self.root.after(1800, self._poll_status)

    def _toggle_engine(self) -> None:
        if self.app is not None and self.app.is_running:
            self._stop_engine()
        else:
            self._start_engine()

    def _start_engine(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.settings = self._collect()
        save_settings(self.settings)
        restore_cursor_visibility()
        user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)
        self.app = SmoothCursorApp(self.settings)

        def _run() -> None:
            try:
                assert self.app is not None
                self.app.run(quiet=True)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.root.after(0, lambda: messagebox.showerror("Smooth Cursor", err))

        self._thread = threading.Thread(target=_run, name="smooth-cursor", daemon=True)
        self._thread.start()
        self._engine_btn.configure(text="Stop")
        self._status_text.set("Running")
        self._status_pill.configure(fg_color=_COLORS["ok"])
        self._update_tray_menu()

    def _stop_engine(self) -> None:
        if self.app is not None:
            self.app.stop()
        self._engine_btn.configure(text="Start")
        self._status_text.set("Stopping…")
        self._status_pill.configure(fg_color=_COLORS["muted"])
        self.root.after(300, self._poll_status)
        self._update_tray_menu()

    def _poll_status(self) -> None:
        if self._quitting:
            return
        running = self.app is not None and self.app.is_running
        current = self._status_text.get()
        if running:
            if not current.startswith("Saved") and current != "Defaults restored":
                self._status_text.set("Running")
                self._status_pill.configure(fg_color=_COLORS["ok"])
            self._engine_btn.configure(text="Stop")
        else:
            if not current.startswith("Saved") and current != "Defaults restored":
                self._status_text.set("Stopped")
                self._status_pill.configure(fg_color=_COLORS["muted"])
            self._engine_btn.configure(text="Start")
        self._update_tray_menu()
        self.root.after(500, self._poll_status)

    # --- Tray ---

    def _setup_tray(self) -> None:
        try:
            import pystray
            from pystray import MenuItem as Item
        except ImportError:
            self._tray = None
            return

        menu = pystray.Menu(
            Item("Open settings", self._tray_show, default=True),
            Item("Stop" if (self.app and self.app.is_running) else "Start", self._tray_toggle),
            Item("Quit", self._tray_quit),
        )
        self._tray = pystray.Icon(
            "SmoothCursor",
            _tray_icon_image(),
            "Smooth Cursor",
            menu,
        )

        def _run_tray() -> None:
            assert self._tray is not None
            self._tray.run()

        self._tray_thread = threading.Thread(target=_run_tray, name="smooth-tray", daemon=True)
        self._tray_thread.start()

    def _update_tray_menu(self) -> None:
        if self._tray is None:
            return
        try:
            import pystray
            from pystray import MenuItem as Item

            running = self.app is not None and self.app.is_running
            self._tray.menu = pystray.Menu(
                Item("Open settings", self._tray_show, default=True),
                Item("Stop" if running else "Start", self._tray_toggle),
                Item("Quit", self._tray_quit),
            )
            self._tray.update_menu()
        except Exception:
            pass

    def _tray_show(self, _icon=None, _item=None) -> None:
        self.root.after(0, self._show_window)

    def _tray_toggle(self, _icon=None, _item=None) -> None:
        self.root.after(0, self._toggle_engine)

    def _tray_quit(self, _icon=None, _item=None) -> None:
        self.root.after(0, self._quit_app)

    def _hide_to_tray(self, *, silent: bool = False) -> None:
        if self._tray is None:
            self._show_window()
            if not silent:
                messagebox.showwarning(
                    "Smooth Cursor",
                    "System tray is unavailable (install pystray). Window stays open.",
                )
            return
        self.root.withdraw()

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        try:
            self.root.attributes("-topmost", True)
            self.root.after(200, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

    def _quit_app(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        if self.app is not None:
            self.app.stop()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()
        if self.app is not None and self.app.is_running:
            self.app.stop()
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass


def run_gui() -> None:
    SmoothCursorGUI().run()
