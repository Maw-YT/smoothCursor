"""Launcher for Smooth Cursor (GUI by default; --cli for console)."""

import sys

from smooth_cursor.app import main

if __name__ == "__main__":
    gui = "--cli" not in sys.argv and "--no-gui" not in sys.argv
    main(gui=gui)
