# Smooth Cursor

Windows cursor effects: movement inertia, click/scroll animation, typing badges, animated `.ani` scheme cursors, plus configurable **trails** and **dust** particles.

## Features

- **Inertia** — tilt and optional speed-based size from mouse velocity
- **Click & scroll** — springy scale and twist feedback
- **Typing badges** — key / shortcut labels on the cursor (Ctrl+C, Win+E, …)
- **Animated cursors** — Wait/Busy and other scheme `.ani` files play automatically; optional custom `.ani` override
- **Trails** — soft fading stamps behind the pointer (color, size, spacing, fade, min speed)
- **Dust** — particles when moving fast or clicking (size, life, gravity, spread, drag, themes)
- **System tray GUI** — live settings; close hides to tray

## Requirements

- Windows 10/11
- Python 3.11+ (for source runs)

## Quick start

### Prebuilt exe

Run `dist\SmoothCursor.exe` (build it first — see below). Settings live in `%APPDATA%\SmoothCursor\settings.json`.

### From source

```bat
pip install -r requirements.txt
python run.py
```

CLI-only (no GUI):

```bat
python run.py --cli
```

## Build the exe

```bat
build_exe.bat
```

Or:

```bat
python build_exe.py
```

Output: `dist\SmoothCursor.exe`

## Settings

Open the tray icon → settings window. Tabs cover General masters, Inertia, Click, Scroll, Typing, Ani, Effects (trails/dust), and Twist.

## Notes

- Hardware cursors are reshaped for inertia/click/typing; trails and dust use a click-through layered overlay.
- Opaque black / mask-style cursors may pass through without effects so apps stay usable.
- Quit from the tray or Stop in the GUI to restore system cursors.
