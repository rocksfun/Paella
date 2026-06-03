# PyInstaller runtime hook — load bundled Qt DLLs before PySide6 import (Windows)
import os
import sys

if sys.platform == "win32" and hasattr(sys, "_MEIPASS"):
    base = sys._MEIPASS
    for sub in ("", "PySide6", os.path.join("PySide6", "Qt6"), os.path.join("PySide6", "plugins")):
        path = os.path.join(base, sub) if sub else base
        if os.path.isdir(path):
            try:
                os.add_dll_directory(path)
            except (AttributeError, OSError):
                pass
    os.environ.setdefault("QT_PLUGIN_PATH", os.path.join(base, "PySide6", "plugins"))
