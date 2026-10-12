"""Writes errors to a log file, because the windowed .exe has no console to print to."""
import logging
import sys 
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from paths import data_path

LOG_FILE = Path(data_path("app.log"))
log = logging.getLogger("barangay")   # use in the app: log.info / log.warning / log.exception
_configured = False

class _IgnoreWindowsReset(logging.Filter):
    """The WebView closing a connection raises WinError 10054 inside asyncio. It's harmless noise
    that buries real errors, so drop that one case only."""
    def filter(self, record):
        exc = record.exc_info[1] if record.exc_info else None
        return not (isinstance(exc, ConnectionResetError) and getattr(exc, "winerror", None) == 10054)
    
def setup_logging():
    """Safe to call more than once; only the first call does anything."""
    global _configured
    if _configured:
        return
    _configured = True

    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    root = logging.getLogger()
    root.setLevel(logging.WARNING)   # libraries (uvicorn, SQLAlchemy...): warnings and errors only
    log.setLevel(logging.INFO)       # our own code: info and above

    try:
        # 1 MB per file, 5 old files kept: the log can never grow past about 6 MB
        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        pass   # can't write the file (read-only folder?): carry on, never stop the app for a log

    if sys.stderr is not None:   # a windowed .exe has no console; a terminal run does
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)

    def log_uncaught(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Uncaught error", exc_info=(exc_type, exc, tb))

    def log_thread_error(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "unknown"
        log.critical("Uncaught error in thread %s", name,
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = log_uncaught
    threading.excepthook = log_thread_error

    log.info("Logging started")
    logging.getLogger("asyncio").addFilter(_IgnoreWindowsReset())
