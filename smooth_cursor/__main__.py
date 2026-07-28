from .app import main
import sys

if __name__ == "__main__":
    gui = "--cli" not in sys.argv and "--no-gui" not in sys.argv
    main(gui=gui)
