"""Where the app finds its files, both in development and when packaged with PyInstaller.

Two kinds of files:
  * bundled files that only get READ  (templates/, static/, app_icon.ico)
      -> inside the PyInstaller bundle, found with resource_path()
  * files the app WRITES               (the database, last_ticket_printed.txt)
      -> a folder the user can always write to, found with data_path()

Installed apps usually live in "C:\\Program Files", which a normal user cannot write to,
so the database must not be stored next to the program there.
"""
import os
import sys
from pathlib import Path

APP_NAME = "BarangayQueueManagement"

# PyInstaller sets sys.frozen, and sys._MEIPASS = the folder holding the bundled files
FROZEN = getattr(sys, "frozen", False)
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

if FROZEN:
    # Packaged app: C:\Users\<user>\AppData\Local\BarangayQueue
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_NAME
else:
    # Development: keep using the project folder, so your current database still works
    DATA_DIR = Path(__file__).resolve().parent

DATA_DIR.mkdir(parents=True, exist_ok=True)


def resource_path(relative: str) -> str:
    """Absolute path of a bundled, read-only file or folder, e.g. resource_path("templates")."""
    return str(BUNDLE_DIR / relative)


def data_path(name: str) -> str:
    """Absolute path of a file the app writes, e.g. data_path("barangay_system.db")."""
    return str(DATA_DIR / name)
