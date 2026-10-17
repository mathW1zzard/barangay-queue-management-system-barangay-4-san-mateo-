from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi import Request 
from fastapi import Form
from fastapi import Depends
from fastapi.responses import RedirectResponse, Response
from app_logging import setup_logging, log
setup_logging()
from database import SessionLocal, Ticket, Service, Visit, Admin, AdminLog, engine
from paths import resource_path, data_path
from datetime import datetime, date, time, timedelta
from starlette.middleware.sessions import SessionMiddleware
from enum import Enum
import threading
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi import WebSocket, WebSocketDisconnect
from escpos.printer import Dummy
import json
import sys
import textwrap
import unicodedata
from collections import Counter
from sqlalchemy import desc, func, text
from pathlib import Path
import bcrypt
import csv
import io
import sqlite3
import asyncio
import math
import re
import os
from time import monotonic          
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from sqlalchemy.exc import IntegrityError

# For authentication(recovery key)
import secrets
import string

main_loop: asyncio.AbstractEventLoop | None = None


@asynccontextmanager
async def lifespan(_app):
    global main_loop
    main_loop = asyncio.get_running_loop()        # remember uvicorn's loop for worker threads
    yield

app = FastAPI(lifespan=lifespan)

def load_secret_key() -> str:
    key_file = Path(data_path("session_secret.key"))
    try:
        key = key_file.read_text(encoding="utf-8").strip()
        if len(key) >= 64:
            return key
        log.warning("Session secret was damaged; generating a new one.")
    except OSError:
        pass                                   # missing file: first run

    key = secrets.token_urlsafe(64)
    tmp = key_file.with_suffix(".tmp")
    tmp.write_text(key, encoding="utf-8")
    os.replace(tmp, key_file)                  # atomic: never leaves a half-written key
    return key

app.add_middleware(SessionMiddleware,
    secret_key=load_secret_key(),
    session_cookie="bqs_session", 
    same_site="lax",
    max_age=8 * 60 * 60,
)

app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

@app.middleware("http")
async def block_cross_origin_posts(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host", ""):
            return Response("Cross-origin request blocked", status_code=403)
    return await call_next(request)

templates = Jinja2Templates(directory=resource_path("templates"))

PAGE_SIZE = 15

def render_registration(request: Request, error: str | None = None, form: dict | None = None, status_code: int = 200):
    with SessionLocal() as db:
        today_start = datetime.combine(date.today(), time.min)
        tickets = db.query(Ticket).filter(Ticket.created_at >= today_start).order_by(desc(Ticket.created_at)).all()
        services = db.query(Service).filter(Service.service_status == "Active").all()
        return templates.TemplateResponse(request, "client_registration.html", {
            "tickets": tickets, "services": services, "total_today": len(tickets),
            "queue_info": queue_snapshot(), "error": error, "form": form,
            "auto_print": AUTO_PRINT_ON_REGISTER,
        }, status_code=status_code)


def current_priority_streak(db) -> int:
    """Consecutive Priority calls at the end of today's call order (0, 1 or 2).
    Derived from called_at, so a restart cannot reset it."""
    start = datetime.combine(date.today(), time.min)
    calls = (db.query(Ticket.lane, Ticket.called_at).filter(Ticket.called_at >= start).all()
             + db.query(Visit.lane, Visit.called_at).filter(Visit.called_at >= start).all())
    streak = 0
    for lane, _ in sorted(calls, key=lambda r: r[1], reverse=True)[:2]:
        if lane != "P":
            break
        streak += 1
    return streak

THROTTLE_MAX_FAILURES, THROTTLE_WINDOW_SECONDS = 5, 60

# ADMIN LOGS
LOG_CATEGORIES = ["Queue", "Service", "Settings", "Security", "Database"]
NOT_SIGNED_IN = "(not signed in)" # who gets blamed for failed logins

# SESSION TRACKING (so the audit log can record a session that ended by closing the app)
active_admin_id = None
SESSION_EVENTS = ("Login", "Logout", "Session Closed", "Session Interrupted")
SESSION_SECRET = load_secret_key() 

# DAILY ROLLOVER (tickets left over from a previous day)
last_rollover_date = None            # the day the cleanup last ran in this session
last_backup_attempt = None
rollover_retry_at = 0.0
rollover_failed = False
ROLLOVER_RETRY_SECONDS = 300
rollover_lock = threading.Lock()

# MAINTENANCE
BACKUP_DIR_DEFAULT = Path(data_path("backups"))
PURGE_MIN_AGE_DAYS = 30 # safety: records newer than this can never be purged

# AUTOMATIC BACKUPS
AUTO_BACKUP_KEEP_DAYS = 14   # keep the newest automatic backup of each of the last 14 days that have one
AUTO_BACKUP_NAME = re.compile(r"^auto_(\d{8})_\d{6}(?:_\d+)?\.db$")   # the ONLY file names cleanup may ever delete
EXTRA_BACKUP_SETTING = Path(data_path("backup_folder.txt"))   # one line: full path of a second backup folder

def flash(request: Request, ok: bool, text_: str):
    """One-time message shown on the next page load (stored in the session cookie)."""
    request.session["flash"] = {"ok": ok, "text": text_}
    
def log_action(category: str, action: str, description: str = "", request: Request = None, username: str = None):
    """Write one row to the admin audit log. Call it AFTER the action succeeded.

    Who did it: pass `request` (the signed-in admin is looked up from the session) or pass
    `username` directly (login, recovery, background events). With neither, it is logged as "system".
    It never raises: a logging problem must not stop the real action (like calling a ticket).
    """
    try:
        db = SessionLocal()
        try:
            if username is None and request is not None:
                admin_id = request.session.get("admin_id")
                admin = db.query(Admin).filter(Admin.id == admin_id).first() if admin_id else None
                username = admin.username if admin else None
            db.add(AdminLog(
                admin_username=username or "system",
                category=category,
                action=action,
                description=description,
                created_at=datetime.now(),
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        log.exception("Audit log failed")

def log_action_throttled(request: Request, category: str, action: str, description: str, seconds: int = 60):
    """Log at most once per `seconds` per session for the same action (refreshing a page is not a new event)."""
    now = datetime.now().timestamp()
    key = f"last_log_{action}"
    if now - request.session.get(key, 0) < seconds:
        return
    request.session[key] = now
    log_action(category, action, description, request)
    
def log_app_start():
    """Run once when the program starts (called from run_app.py).

    1. If an admin's last session event is a "Login" with nothing after it, the previous run ended
       without a logout and without a normal window close (crash, power cut, killed in Task Manager).
       Record that now. The real end time is unknown, so the entry says it was detected at startup.
    2. Record that the application started.
    """
    try:
        db = SessionLocal()
        try:
            names = [r[0] for r in db.query(AdminLog.admin_username).filter(AdminLog.action == "Login").distinct().all()]
            unfinished = []
            for name in names:
                last = db.query(AdminLog).filter(AdminLog.admin_username == name, AdminLog.category == "Security", AdminLog.action.in_(SESSION_EVENTS)).order_by(AdminLog.id.desc()).first()
                
                if last and last.action == "Login":
                    unfinished.append((name, last.created_at))
        finally:
            db.close()            

        for name, since in unfinished:
            began = since.strftime("%m/%d/%Y %I:%M %p") if since else "an earlier time"
            log_action("Security", "Session Interrupted",
                       f"Session that began {began} was never closed (power loss, crash or forced shutdown). "
                       f"Detected at startup, so the real end time is unknown.", username=name)
        log_action("Security", "Application Started", "Application started", username="system")
        
    except Exception:
        log.exception("Startup audit check failed")
  
def log_app_closed():
    """Run after the app window has closed (called from run_app.py). Logs it only if someone was still signed in."""
    if active_admin_id is None:
        return
    try:
        db = SessionLocal()
        try:
            admin = db.query(Admin).filter(Admin.id == active_admin_id).first()
            name = admin.username if admin else None
        finally:
            db.close()
        if name:
            log_action("Security", "Session Closed", "Application was closed without signing out", username=name)
    except Exception as e:
        log.exception("Closed audit log failed")
          
def parse_date(value: str):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None

def archive_stale_tickets() -> int:
    """Tickets registered on an earlier day can never be served today (numbers restart every day).

    Save each one in attendance history as a no-show, on the day it was registered, and remove it
    from the live queue. All in one transaction. Returns how many tickets were archived.
    """

    today_start = datetime.combine(date.today(), time.min)
    with SessionLocal() as db:
        stale = db.query(Ticket).filter(
            Ticket.created_at < today_start,
            Ticket.status.in_(["waiting", "called", "skipped"]),
        ).all()
        if not stale:
            return 0

        counts = Counter(t.status for t in stale)
        days = sorted({t.created_at.date() for t in stale})
        total = len(stale)
        for t in stale:
            db.add(Visit(
                citizen_name=t.citizen_name, lane=t.lane, ticket_number=t.ticket_number,
                final_status="no_show", service_id=t.service_id,
                created_at=t.created_at,      # stays on the day it was registered
                called_at=t.called_at,
                completed_at=None,            # never served, so no time out ("--" on the pages)
            ))
            db.delete(t)
        db.commit()

    log_action("Queue", "Old Tickets Archived",
               f"{total} ticket(s) left over from {range_label(days[0], days[-1])} were saved as no-show "
               f"({counts['waiting']} waiting, {counts['called']} called, {counts['skipped']} skipped)",
               username="system")
    return total

def create_backup_file(target_dir: Path, prefix: str) -> Path:
    """Copy the live database into target_dir with SQLite's backup API, then check the copy.

    It is written under a temporary name and renamed only when the check passes, so a half-written
    file (power cut, unplugged drive) can never be mistaken for a real backup. Raises on any problem.
    """
    stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
    target, n = target_dir / f"{prefix}{stamp}.db", 1
    while target.exists():
        target = target_dir / f"{prefix}{stamp}_{n}.db"
        n += 1
    
    temp = target.with_suffix(".tmp")
    try:
        source = sqlite3.connect(data_path("barangay_system.db"))
        try:
            dest = sqlite3.connect(str(temp))
            try:
                source.backup(dest)
                verdict = dest.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                dest.close()
        finally:
            source.close()
        if verdict != "ok":
            raise RuntimeError(f"the copy failed its integrity check ({verdict})")
        temp.replace(target)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return target

def _today_start_str() -> str:
    return datetime.combine(date.today(), time.min).strftime("%Y-%m-%d %H:%M:%S.%f")  # same format SQLAlchemy stores

_CLAIM = text("""UPDATE tickets SET status='called', called_at=COALESCE(called_at, :now)
                 WHERE id=:id AND status=:from_status
                   AND NOT EXISTS (SELECT 1 FROM tickets
                                   WHERE status='called' AND created_at >= :today_start)""")

def get_extra_backup_dir():
    """The optional second backup folder (an external drive, say), or None."""
    try:
        value = EXTRA_BACKUP_SETTING.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(value) if value else None


def list_auto_backups(folder: Path):
    """Our own automatic backups in a folder, newest first."""
    try:
        files = [f for f in folder.iterdir() if f.is_file() and AUTO_BACKUP_NAME.match(f.name)]
    except OSError:
        return []
    return sorted(files, key=lambda f: f.name, reverse=True)   # the names start with the date and time


def prune_auto_backups(folder: Path):
    """Keep the newest automatic backup of each of the last AUTO_BACKUP_KEEP_DAYS days that have one.

    Only files named exactly like our automatic backups are touched: manual backups and anything
    else in the folder are never deleted. A day without a backup is skipped, not counted, so a
    long break from using the app never wipes out the only backups.
    """
    try:
        kept_days = set()
        for f in list_auto_backups(folder):   # newest first
            day = AUTO_BACKUP_NAME.match(f.name).group(1)
            if day not in kept_days and len(kept_days) < AUTO_BACKUP_KEEP_DAYS:
                kept_days.add(day)            # the newest backup of a recent day: keep
            else:
                f.unlink(missing_ok=True)     # an older backup of the same day, or a day past the limit
        for leftover in folder.glob("auto_*.tmp"):
            leftover.unlink(missing_ok=True)  # a backup that was interrupted half way
    except OSError:
        log.exception("Cleaning up old backups failed")


def auto_backup(reason: str) -> bool:
    """Make an automatic backup, plus a copy in the second folder if one is set. Never raises.

    Returns True if at least one copy was saved. Everything is recorded in the audit log.
    """
    saved, problems = [], []

    try:
        BACKUP_DIR_DEFAULT.mkdir(parents=True, exist_ok=True)
        saved.append(str(create_backup_file(BACKUP_DIR_DEFAULT, "auto_")))
        prune_auto_backups(BACKUP_DIR_DEFAULT)
    except Exception as e:
        log.exception("Automatic backup failed")
        problems.append(f"backup failed: {e}")

    extra = get_extra_backup_dir()
    if extra:
        try:
            if not extra.is_dir():
                raise FileNotFoundError(f"folder not found: {extra}")
            saved.append(str(create_backup_file(extra, "auto_")))
            prune_auto_backups(extra)
        except Exception as e:
            log.exception("Automatic backup copy failed")
            problems.append(f"copy to {extra} failed: {e}")

    if saved:
        log_action("Database", "Auto Backup", f"{reason}: saved " + "; ".join(saved), username="system")
    if problems:
        log_action("Database", "Backup Failed", f"{reason}: " + "; ".join(problems), username="system")
    return bool(saved)

def ensure_daily_rollover():
    """Runs the cleanup above once per calendar day, whenever the first page of that day is opened.

    This also covers a computer that stays on past midnight. After the first run of the day it costs
    one date comparison.
    """
    global last_rollover_date, last_backup_attempt, rollover_retry_at, rollover_failed
    today = date.today()
    if last_rollover_date == today or monotonic() < rollover_retry_at:
        return
    with rollover_lock:
        if last_rollover_date == today or monotonic() < rollover_retry_at:
            return
        if last_backup_attempt != today:
          last_backup_attempt = today
          auto_backup("Daily Backup")  
        try:
            archive_stale_tickets()
        except Exception:
            log.exception("Daily rollover failed")
            rollover_failed = True
            rollover_retry_at = monotonic() + ROLLOVER_RETRY_SECONDS
            return
        rollover_failed = False
        last_rollover_date = today
        
templates.env.globals["rollover_failed"] = lambda: rollover_failed

def active_queue_today():
    """Tickets registered today that are still in the queue."""
    db = SessionLocal()
    
    try:
        today_start = datetime.combine(date.today(), datetime.min.time())
        return db.query(Ticket).filter(
            Ticket.created_at >= today_start,
            Ticket.status.in_(["waiting", "called", "skipped"]),
        ).count()
    finally:
        db.close()
        
templates.env.globals["active_queue_today"] = active_queue_today
BARANGAY_NAME = "Barangay 4, San Mateo, Isabela"
templates.env.globals["barangay_name"] = BARANGAY_NAME

class StatusEnum(str, Enum):
    ACTIVE = "Active"
    INACTIVE = "Inactive"
    
app.mount("/static", StaticFiles(directory=resource_path("static")), name="static")

# WEBSOCKET
class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()
        
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active.add(websocket)
        
    def disconnect(self, websocket: WebSocket):
            self.active.discard(websocket)
            
    async def broadcast(self, message: str):
        dead = []
        for ws in list(self.active):              # iterate a copy
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)                   # one bad socket no longer stops the rest
        for ws in dead:
            self.active.discard(ws)
            
manager = ConnectionManager()

@app.websocket("/ws/display")
async def websocket_display(websocket: WebSocket):
    origin = websocket.headers.get("origin")
    if origin and urlparse(origin).netloc != websocket.headers.get("host", ""):
        await websocket.close(code=1008)          # refuse pages from other origins
        return
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)             # also runs on unexpected errors, so no leak


def notify_display_update():
    """Safe to call from the normal (threaded) routes."""
    if main_loop is not None and main_loop.is_running():
        asyncio.run_coroutine_threadsafe(manager.broadcast("update"), main_loop)
    
# ANNOUNCEMENT AUDIO
def build_announcement_clips(ticket_number: str):
    """Convert a ticket number such as P-047 into a list of reusable audio clips."""

    m = TICKET_RE.match(ticket_number or "")
    if not m:
        return []
    letter, digits = m.groups()
    return ["ticket_number", f"letters/{letter}", *[f"digits/{d}" for d in digits], "proceed_to_counter"]

       
# True  = the slip prints by itself when a ticket is registered (the confirmation page then
#         offers "Print again").
# False = nothing prints until "Print ticket slip" is pressed on the confirmation page.


# PRINTER
AUTO_PRINT_ON_REGISTER = True
PRINTER_SETTINGS_FILE = Path(data_path("printer_settings.json"))
PAPER_COLUMNS = {"58": 32, "80": 48}           # characters per line on 58 mm and 80 mm paper
VIRTUAL_PRINTER_WORDS = ("pdf", "xps", "onenote", "fax", "document writer")
CHECK_PRINTER_STATUS = True                    # set False if Windows wrongly reports your printer as offline
HAS_CUTTER = False            # tear-off printer; set True only if yours has a blade
FEED_LINES = 4                # blank lines fed after the slip so it clears the tear bar
CANCEL_CHINESE_MODE = True    # some Xprinter units start in Chinese (GBK) mode, which can garble Ñ and é

# On a non-Windows computer (or with BQS_DUMMY_PRINTER=1) slips are written to a file instead of printed.
USE_DUMMY_PRINTER = sys.platform != "win32" or bool(os.environ.get("BQS_DUMMY_PRINTER"))

# Windows printer status bits (winspool.h) and what to tell the clerk
_STATUS_PROBLEMS = {
    0x1: "is paused", 0x2: "reports an error", 0x8: "has a paper jam", 0x10: "is out of paper",
    0x40: "has a paper problem", 0x80: "is offline", 0x1000: "is not available",
    0x400000: "has its cover open",
}
_ATTRIBUTE_WORK_OFFLINE = 0x400


class PrinterError(Exception):
    """A printing problem, worded so a clerk can understand it."""


def load_printer_settings() -> dict:
    try:
        data = json.loads(PRINTER_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    paper = str(data.get("paper_mm", "58"))
    return {"printer_name": str(data.get("printer_name", "")).strip(),
            "paper_mm": paper if paper in PAPER_COLUMNS else "58"}


def save_printer_settings(printer_name: str, paper_mm: str) -> None:
    tmp = PRINTER_SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"printer_name": printer_name, "paper_mm": paper_mm}), encoding="utf-8")
    os.replace(tmp, PRINTER_SETTINGS_FILE)       # atomic, like the session key


def list_windows_printers() -> list[str]:
    """Printers installed in Windows, without the virtual ones (PDF, XPS, Fax...)."""
    if sys.platform != "win32":
        return []
    try:
        import win32print
        found = win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)
        names = [p[2] for p in found]            # each entry: (flags, description, name, comment)
    except Exception:
        log.exception("Could not list the Windows printers")
        return []
    return sorted(n for n in names if not any(w in n.lower() for w in VIRTUAL_PRINTER_WORDS))


def send_to_windows_printer(printer_name: str, data: bytes) -> None:
    """Hand raw ESC/POS bytes to the Windows print spooler. Raises PrinterError with a plain message."""
    import win32print
    try:
        handle = win32print.OpenPrinter(printer_name)
    except Exception as e:
        raise PrinterError(f'Windows cannot open the printer "{printer_name}". '
                           f"Is it installed and switched on? ({e})") from e
    try:
        if CHECK_PRINTER_STATUS:
            info = win32print.GetPrinter(handle, 2)
            problems = [msg for bit, msg in _STATUS_PROBLEMS.items() if info.get("Status", 0) & bit]
            if info.get("Attributes", 0) & _ATTRIBUTE_WORK_OFFLINE:
                problems.append('is set to "Use Printer Offline"')
            if problems:
                # A job sent now would sit in the queue and print later, maybe twice if the clerk retries.
                raise PrinterError(f'The printer "{printer_name}" ' + " and ".join(problems) + ".")
        win32print.StartDocPrinter(handle, 1, ("Queue slip", None, "RAW"))
        try:
            win32print.StartPagePrinter(handle)
            win32print.WritePrinter(handle, data)
            win32print.EndPagePrinter(handle)
        finally:
            win32print.EndDocPrinter(handle)
    finally:
        win32print.ClosePrinter(handle)


def deliver_to_printer(data: bytes, settings: dict) -> None:
    if USE_DUMMY_PRINTER:
        with open(data_path("last_ticket_printed.txt"), "wb") as f:      # contains client names: test mode only
            f.write(data)
        log.info("Dummy printer: slip saved to last_ticket_printed.txt")
        return
    if not settings["printer_name"]:
        raise PrinterError("No printer is selected. Choose one under Maintenance > Receipt printer.")
    send_to_windows_printer(settings["printer_name"], data)


def slip_text(value: str) -> str:
    """Keeps letters the printer's default code page (CP437: Ñ ñ é í ó ú...) can print.
    Anything else is reduced to plain letters, so it never prints as garbage."""
    def printable(ch: str) -> str:
        try:
            ch.encode("cp437")
            return ch
        except UnicodeEncodeError:
            return unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode()
    return "".join(printable(c) for c in unicodedata.normalize("NFC", value or ""))

SIZE_NORMAL = (1, 1)
SIZE_HEADER = (1, 2)    # barangay name: taller, same width
SIZE_TICKET = (5, 5)    # "P-008" = 25 columns; even "P-1000" = 30 still fits
SIZE_LANE   = (2, 2)    # "PRIORITY LANE" = 26 columns
SIZE_BODY   = (1, 2)    # client, service, date, footer: taller, same width, so names wrap no more than before

def _style(p, align="left", bold=False, size=SIZE_NORMAL):
    """Set alignment, bold and character size. Size stays until changed, so every block sets it."""
    p.set(align=align, bold=bold)
    w, h = size
    p._raw(b"\x1d!" + bytes([((w - 1) << 4) | (h - 1)]))     # GS ! n: width in the high bits, height in the low
    
    
def build_slip(ticket_number: str, citizen_name: str, service_name: str, lane: str,
               created_at: datetime, columns: int) -> bytes:
    p = Dummy()                                   # only builds the ESC/POS bytes; nothing is printed here
    p._raw(b"\x1b@")                              # reset the printer to a known state
    if CANCEL_CHINESE_MODE:
        p._raw(b"\x1c\x2e")                       # FS . = cancel Chinese character mode
    rule = "-" * columns + "\n"

    def fill(value: str, size: tuple, label: str = "") -> str:
        # Larger text means fewer characters per line, so wrap at columns / width.
        width = max(8, columns // size[0])
        return textwrap.fill(label + slip_text(value), width=width, subsequent_indent=" " * len(label)) + "\n"

    _style(p, "center", True, SIZE_HEADER)
    p.text(fill(BARANGAY_NAME.upper(), SIZE_HEADER))
    _style(p, "center", False, SIZE_NORMAL)
    p.text("Official Queue Slip\n")
    p.text(rule)

    _style(p, "center", True, SIZE_TICKET)
    p.text(f"{ticket_number}\n")
    _style(p, "center", True, SIZE_LANE)
    p.text(("PRIORITY LANE" if lane == "P" else "REGULAR LANE") + "\n")
    _style(p, "center", False, SIZE_NORMAL)
    p.text(rule)

    _style(p, "left", False, SIZE_BODY)
    p.text(fill(citizen_name, SIZE_BODY, "Client : "))
    p.text(fill(service_name, SIZE_BODY, "Service: "))
    p.text(fill(created_at.strftime("%b %d, %Y %I:%M %p"), SIZE_BODY, "Date   : "))
    _style(p, "center", False, SIZE_NORMAL)
    p.text(rule)

    _style(p, "center", False, SIZE_BODY)
    p.text("Please wait for your number\nto be called. Thank you!\n")

    _style(p, "center", False, SIZE_NORMAL)        # back to 1x so the feed below is a normal length
    p.text("\n" * FEED_LINES)
    if HAS_CUTTER:
        p.cut()
    return p.output


def build_test_slip(columns: int) -> bytes:
    p = Dummy()
    p._raw(b"\x1b@")
    
    if CANCEL_CHINESE_MODE:
        p._raw(b"\x1c\x2e")                       # FS . = cancel Chinese character mode
    
    p.set(align="center", bold=True, width=2, height=2)
    p.text("TEST\n")
    p.set(align="center", bold=False, width=1, height=1)
    p.text("The printer is working\n")
    p.text("-" * columns + "\n")
    p.set(align="left", bold=False, width=1, height=1)
    p.text(("1234567890" * 6)[:columns] + "\n")  # must fit on ONE row; if it wraps, the paper width setting is too big
    p.text(slip_text("Accents: \u00d1 \u00f1 \u00e9 \u00ed \u00f3 \u00fa") + "\n")
    p.text(f"Paper setting: {columns} characters per line\n")
    
    p.text("\n" * FEED_LINES)
    if HAS_CUTTER:
        p.cut()
    p.cut()
    return p.output


def print_ticket(ticket_number: str, citizen_name: str, service_name: str, lane: str, created_at: datetime) -> bool:
    """Send the queue slip to the printer. Returns True if it was handed over, False if it failed."""
    try:
        settings = load_printer_settings()
        data = build_slip(ticket_number, citizen_name, service_name, lane, created_at,
                          PAPER_COLUMNS[settings["paper_mm"]])
        deliver_to_printer(data, settings)
        return True
    except Exception as e:
        log.exception("Ticket printing failed for %s", ticket_number)
        log_action("Settings", "Print Error", f"Ticket slip {ticket_number} failed to print: {e}", username="system")
        return False

_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
TICKET_RE = re.compile(r"^([PR])-(\d{1,4})$")

def clean_text(value: str, max_len: int) -> str:
    """Strips control characters (they would reach the ESC/POS printer), collapses whitespace, caps length."""
    return " ".join(_CTRL.sub(" ", value or "").split())[:max_len]

def parse_optional_int(value: str):
    try:
        return int(value) if value and value.strip() else None
    except ValueError:
        return None
    
@app.get("/")
def read_root():
    with SessionLocal() as db:
        user_exists = db.query(Admin).first()
    if not user_exists:
        return RedirectResponse(url="/setup", status_code=303)
    return RedirectResponse(url="/login", status_code=303)

@app.get("/setup")
def show_setup(request: Request):
    with SessionLocal() as db:
        existing_admin = db.query(Admin).first()

    if existing_admin:
        return RedirectResponse(url="/login", status_code=303)

    return templates.TemplateResponse(request, "setup1.html", {})

def normalize_username(value: str) -> str:
    return clean_text(value, 50)
 
@app.post("/setup")
def process_setup(request: Request, username: str = Form(...), password: str = Form(...), confirm_password: str = Form(...)):
    username = clean_text(username, 50)
    
    if not username: 
        return templates.TemplateResponse(request, "setup1.html",{"error": "Username is required."}, status_code=400)

    error = validate_new_password(password, confirm_password)

    if error:
        return templates.TemplateResponse(request, "setup1.html", {"error": error},status_code=400)

    recovery_key = generate_recovery_key()
    db = SessionLocal()
    try:
        # Acquire a write reservation before checking for an admin.
        db.execute(text("BEGIN IMMEDIATE"))

        if db.query(Admin).first() is not None:
            db.rollback()

            return RedirectResponse(url="/login", status_code=303)
            
        new_admin = Admin(username=username, password_hash=hash_password(password), recovery_key_hash=hash_password(recovery_key))
        db.add(new_admin)
        db.commit()
        
    except IntegrityError:
        db.rollback()
        return templates.TemplateResponse(request,"setup1.html",{"error": "The account could not be created. Please try again."}, status_code=409)

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    log_action("Security", "Account Created", f'Created admin account "{username}" (first-time setup)', username=username)
    
    response = templates.TemplateResponse(request, "setup2.html", {"recovery_key": recovery_key})
    return response


@app.get("/login")
def show_login(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/login")
def process_login(request: Request, username: str = Form(...), password: str = Form(...)):
    wait = throttle_wait("login")
    username = normalize_username(username)
    if wait:
        return templates.TemplateResponse(request, "login.html",
            {"error": f"Too many failed attempts. Try again in {wait} seconds."}, status_code=429)

    with SessionLocal() as db:
        admin = db.query(Admin).filter(Admin.username == username).first()

    if not check_password(password, admin.password_hash if admin else None):
        record_failure("login")
        log_action("Security", "Login Failed",
                   f'Failed login attempt for username "{username[:50]}"', username=NOT_SIGNED_IN)
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid username or password"})

    clear_failures("login")
    global active_admin_id
    request.session.clear()
    request.session["admin_id"] = admin.id
    request.session["pwv"] = password_fingerprint(admin.password_hash)
    active_admin_id = admin.id
    log_action("Security", "Login", "Signed in", username=admin.username)
    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/forgot-password")
def show_forgot_password(request: Request):
    return templates.TemplateResponse(request, "forgot_password.html", {})

@app.post("/forgot-password")
def process_forgot_password(
    request: Request,
    username: str = Form(...),
    recovery_key: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    wait = throttle_wait("recovery")
    username = normalize_username(username)
    if wait:
        return templates.TemplateResponse(request, "forgot_password.html",
            {"error": f"Too many attempts. Try again in {wait} seconds."}, status_code=429)

    error, new_recovery_key = None, None
    with SessionLocal() as db:
        admin = db.query(Admin).filter(Admin.username == username).first()
        stored = admin.recovery_key_hash if admin else None
        if not check_password(recovery_key.strip().upper(), stored):
            error = "Invalid username or recovery key"
        else:
            error = validate_new_password(new_password, confirm_password)

        if not error:
            new_recovery_key = generate_recovery_key()
            admin.password_hash = hash_password(new_password)
            admin.recovery_key_hash = hash_password(new_recovery_key)
            db.commit()          # `admin` is still attached to `db` here, so this really saves

    if error:
        if error.startswith("Invalid"):
            record_failure("recovery")
            log_action("Security", "Recovery Failed",
                       f'Failed password reset attempt for username "{username[:50]}"', username=NOT_SIGNED_IN)
        return templates.TemplateResponse(request, "forgot_password.html", {"error": error})

    clear_failures("recovery")
    log_action("Security", "Password Reset",
               "Password reset using the recovery key; a new recovery key was issued", username=username)
    return templates.TemplateResponse(request, "recovery_key_display.html", {"recovery_key": new_recovery_key})

@app.post("/logout")
def logout(request: Request):
    global active_admin_id
    if request.session.get("admin_id"):
        log_action("Security", "Logout", "Signed out", request)   # before the session is cleared
    active_admin_id = None
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)

class NotAuthenticated(Exception):
    pass


@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url="/login", status_code=303)


def login_required(request: Request):
    admin_id = request.session.get("admin_id")
    if not admin_id:
        raise NotAuthenticated()
    with SessionLocal() as db:
        admin = db.get(Admin, admin_id)
        valid = (admin is not None
                 and request.session.get("pwv") == password_fingerprint(admin.password_hash))
    if not valid:                         # account deleted, or password changed/reset elsewhere
        request.session.clear()
        raise NotAuthenticated()
    global active_admin_id
    active_admin_id = admin_id
    ensure_daily_rollover()

@app.get("/change-password", dependencies=[Depends(login_required)])
def show_change_password(request: Request):
    return templates.TemplateResponse(request, "change_password.html", {})


@app.post("/change-password", dependencies=[Depends(login_required)])
def process_change_password(request: Request, current_password: str = Form(...), new_password: str = Form(...), confirm_password: str = Form(...)):
    wait = throttle_wait("change_password")
    if wait:
        return templates.TemplateResponse(request, "change_password.html",
            {"error": f"Too many failed attempts. Try again in {wait} seconds."}, status_code=429)
        
    error = None
    with SessionLocal() as db:
        admin = db.get(Admin, request.session["admin_id"])
        if not admin:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)

        if not check_password(current_password, admin.password_hash):
            error = "Current password is incorrect"
            record_failure("change_password")
        else:
            error = validate_new_password(new_password, confirm_password)

        if not error:
            new_hash = hash_password(new_password)
            admin.password_hash = new_hash
            db.commit()
            request.session["pwv"] = password_fingerprint(admin.password_hash)   # keeps THIS window signed in

        if error:
            if error == "Current password is incorrect":
                log_action("Security", "Password Change Failed", "Wrong current password entered", request)
            return templates.TemplateResponse(request, "change_password.html", {"error": error})

        
    log_action("Security", "Password Changed", "Changed their own password", request)
    return templates.TemplateResponse(request, "change_password.html", {"success": "Password updated."})

def generate_recovery_key():
    """Generates a key like BQS-123A-456B-789C"""
    chars = string.ascii_uppercase + string.digits
    groups = ["".join(secrets.choice(chars) for _ in range(4)) for _ in range(3)]
    return "BQS-" + "-".join(groups)

MAX_PASSWORD_BYTES = 72                      # bcrypt's hard limit
_DUMMY_HASH = bcrypt.hashpw(b"timing-equaliser", bcrypt.gensalt()).decode()

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()

def check_password(plain: str, hashed: str | None) -> bool:
    raw = plain.encode()
    if len(raw) > MAX_PASSWORD_BYTES:
        return False                         # never reaches bcrypt, so no 500
    ok = bcrypt.checkpw(raw, (hashed or _DUMMY_HASH).encode())   # same cost for unknown users
    return ok and hashed is not None

def validate_new_password(new: str, confirm: str) -> str | None:
    if new != confirm:
        return "Passwords do not match"
    if len(new) < 8:
        return "Password must be at least 8 characters"
    if len(new.encode()) > MAX_PASSWORD_BYTES:
        return "Password is too long (72 bytes at most)"
    return None

def password_fingerprint(password_hash: str) -> str:
    return password_hash[-16:]               # changes whenever the password changes

# Throttle: 5 failures per 60 secs per action
THROTTLE_MAX_FAILURES, THROTTLE_WINDOW_SECONDS = 5, 60
_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()

def throttle_wait(key: str) -> int:
    now = monotonic()
    with _failures_lock:
        recent = [t for t in _failures.get(key, []) if now - t < THROTTLE_WINDOW_SECONDS]
        _failures[key] = recent
        if len(recent) >= THROTTLE_MAX_FAILURES:
            return int(THROTTLE_WINDOW_SECONDS - (now - recent[0])) + 1
    return 0

def record_failure(key: str):
    with _failures_lock:
        _failures.setdefault(key, []).append(monotonic())

def clear_failures(key: str):
    with _failures_lock:
        _failures.pop(key, None)
        
        
def get_current_admin(request: Request):
    admin_id = request.session.get("admin_id")
    if not admin_id:
        return None
    with SessionLocal() as db:
        return db.query(Admin).filter(Admin.id == admin_id).first()

def admin_initials(request: Request):
    """Two-letter avatar text from the signed-in username (e.g. admin.secretary -> AS)."""
    admin = get_current_admin(request)
    if not admin or not admin.username:
        return "AD"
    parts = [p for p in re.split(r"[._\s-]+", admin.username) if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return admin.username[:2].upper()


templates.env.globals["admin_initials"] = admin_initials

@app.get("/dashboard", dependencies=[Depends(login_required)])
def show_dashboard(request: Request):
    with SessionLocal() as db:
        
        services = db.query(Service).all()
        
        today_start = datetime.combine(date.today(), datetime.min.time())
        
        active_tickets = db.query(Ticket).filter(Ticket.created_at >= today_start).all()
        visits_today = db.query(Visit).filter(Visit.created_at >= today_start).all()
        
        service_counter = Counter()
        for t in active_tickets:
            service_counter[t.service_id] += 1
        for v in visits_today:
            service_counter[v.service_id] += 1

        service_volume = [
            {"name": s.service_name, "count": service_counter.get(s.id, 0)}
            for s in services
        ]
        service_volume.sort(key=lambda row: row["count"], reverse=True)

        max_count = max((row["count"] for row in service_volume), default=0)
        for row in service_volume:
            row["percent"] = round(row["count"] / max_count * 100) if max_count else 0
            
        # Stat cards
        waiting_count = db.query(Ticket).filter(
            Ticket.status == "waiting", Ticket.created_at >= today_start
        ).count()

        completed_count = db.query(Visit).filter(
            Visit.final_status == "completed", Visit.created_at >= today_start
        ).count()

        no_show_count = db.query(Visit).filter(
            Visit.final_status == "no_show", Visit.created_at >= today_start
        ).count()

        # Issued today = still active + already archived
        total_count = len(active_tickets) + len(visits_today)
        
        overview = []
        for t in active_tickets:
            overview.append({
                "ticket_number": t.ticket_number,
                "citizen_name": t.citizen_name,
                "service_name": t.service_label,
                "status": t.status,
                "sort_time": t.called_at or t.created_at,
            })
        for v in visits_today:
            overview.append({
                "ticket_number": v.ticket_number,
                "citizen_name": v.citizen_name,
                "service_name": v.service_label,
                "status": v.final_status,
                "sort_time": v.completed_at or v.created_at,
            })
            
        
        overview.sort(key=lambda row: row["sort_time"], reverse=True)
        overview = overview[:10]

        response =  templates.TemplateResponse(request, "dashboard.html", {
            "overview": overview,
            "total_count": total_count,
            "waiting_count": waiting_count,
            "completed_count": completed_count,
            "no_show_count": no_show_count,
            "services": services,
            "service_volume": service_volume    
        })
        return response

@app.get("/client_registration", dependencies=[Depends(login_required)])
def show_registration(request: Request):
    return render_registration(request)

def queue_snapshot() -> dict:
    """Numbers for the Live Queue panel on the registration page."""
    
    db = SessionLocal()
    
    try:
        today_start = datetime.combine(date.today(), datetime.min.time())
        
        current = db.query(Ticket).filter(Ticket.status == "called", Ticket.created_at >= today_start) \
            .order_by(Ticket.called_at.desc()).first()
            
        now_serving = current.ticket_number if current else None
        
        def waiting(lane):
            return db.query(Ticket).filter(
                Ticket.status == "waiting", Ticket.lane == lane, Ticket.created_at >= today_start).count()
            
        waiting_priority = waiting("P")
        waiting_regular = waiting("R")
    finally:
        db.close()
        
    upcoming = get_incoming_order(limit=1)
    next_up = upcoming[0].ticket_number if upcoming else None
    
    return {
        "now_serving": now_serving,
        "next_up": next_up,
        "waiting_priority": waiting_priority,
        "waiting_regular": waiting_regular
    }

@app.post("/register", dependencies=[Depends(login_required)])
def register_ticket(request: Request, citizen_name: str = Form(""), lane: str = Form(""), service_id: str = Form("")):
    form = {"citizen_name": citizen_name, "lane": lane, "service_id": service_id}
    name, sid = clean_text(citizen_name, 100), parse_optional_int(service_id)
    if not name:
        return render_registration(request, "Client name is required.", form, 400)
    if lane not in ("P", "R"):
        return render_registration(request, "Please choose Priority or Regular.", form, 400)
    if sid is None:
        return render_registration(request, "Please choose a service.", form, 400)

    with SessionLocal() as db:
        service = db.query(Service).filter(Service.id == sid, Service.service_status == "Active").first()
        if service is None:
            return render_registration(request, "Selected service is unavailable.", form, 400)
        service_name = service.service_name
        ticket_number = issue_ticket_number(db, lane)          
        created_at = datetime.now()
        ticket = Ticket(citizen_name=name, lane=lane, service_id=sid,
                        ticket_number=ticket_number, created_at=created_at)
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id

    url = f"/client_registration/confirmation/{ticket_id}"
    notify_display_update()
    if AUTO_PRINT_ON_REGISTER:
        sent = print_ticket(ticket_number, name, service_name, lane, created_at)
        url += f"?printed={1 if sent else 0}"
    notify_display_update()
    return RedirectResponse(url=url, status_code=303)

@app.get("/client_registration/confirmation/{ticket_id}", dependencies=[Depends(login_required)])
def show_ticket_confirmation(request: Request, ticket_id: int, printed: str = ""):
    """printed: "1" = sent to the printer, "0" = the printer failed, "" = not printed yet."""
    db = SessionLocal()
    try:
        ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if ticket is None:
            # Already served or deleted, so there is nothing left to confirm
            return RedirectResponse(url="/client_registration", status_code=303)
        return templates.TemplateResponse(request, "ticket_confirmation.html", {
            "ticket": ticket,
            "printed": printed,
        })
    finally:
        db.close()


@app.post("/client_registration/confirmation/{ticket_id}/print", dependencies=[Depends(login_required)])
def print_ticket_again(request: Request, ticket_id: int):
    db = SessionLocal()
    try:
        ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if ticket is None:
            return RedirectResponse(url="/client_registration", status_code=303)
        number = ticket.ticket_number
        sent = print_ticket(ticket.ticket_number, ticket.citizen_name,
                            ticket.service_label, ticket.lane, ticket.created_at)
    finally:
        db.close()
    log_action("Queue", "Slip Reprinted", f"Reprinted slip for {number}" + ("" if sent else " (printer error)"), request)
    return RedirectResponse(
        url=f"/client_registration/confirmation/{ticket_id}?printed={1 if sent else 0}",
        status_code=303,
    )

def issue_ticket_number(db, lane: str) -> str:
    """Next number for today's lane. The write takes SQLite's lock, which is held until commit,
    so two registrations can never receive the same number and numbers are never reused."""
    n = db.execute(text("""
        INSERT INTO daily_counters (day, lane, last_number) VALUES (:day, :lane, 1)
        ON CONFLICT (day, lane) DO UPDATE SET last_number = last_number + 1
        RETURNING last_number"""),
        {"day": date.today().isoformat(), "lane": lane}).scalar_one()
    return f"{lane}-{n:03d}"

@app.post("/tickets/delete/{ticket_id}", dependencies=[Depends(login_required)])
def delete_ticket(request: Request, ticket_id: int):
    """Remove a ticket from the live queue. Nothing is written to attendance history."""
    with SessionLocal() as db:
        ticket = db.query(Ticket).filter(Ticket.id == ticket_id, Ticket.status == "waiting").first()
        number = ticket.ticket_number if ticket else None
        if ticket:
            db.delete(ticket)
            db.commit()
    if number:
        log_action("Queue", "Ticket Deleted", f"Removed waiting ticket {number}; not saved to attendance", request)
        notify_display_update()
    return RedirectResponse(url="/client_registration", status_code=303)

# QUEUE MANAGEMENT
@app.get("/queue_management", dependencies=[Depends(login_required)])
def show_queue(request: Request):
    if "announce" in request.query_params:        # the announcement now comes from the session, never the URL
        return RedirectResponse(url="/queue_management", status_code=303)
    
    with SessionLocal() as db:
        tickets = db.query(Ticket).all()
        services = db.query(Service).all()
        upcomming = get_incoming_order(limit=3)
        active_statuses = ["waiting", "called", "skipped"]
        
        today_start = datetime.combine(date.today(), datetime.min.time())
        
        current_ticket = db.query(Ticket).filter(Ticket.status == "called", Ticket.created_at >= today_start) \
            .order_by(Ticket.called_at.desc()).first()

        all_active = db.query(Ticket).filter(Ticket.status.in_(active_statuses), Ticket.created_at >= today_start).count()
        waiting_count = db.query(Ticket).filter(Ticket.status == "waiting", Ticket.created_at >= today_start).count()
        called_count = db.query(Ticket).filter(Ticket.status == "called", Ticket.created_at >= today_start).count()
        skipped_count = db.query(Ticket).filter(Ticket.status == "skipped", Ticket.created_at >= today_start).count()
                
        total_today = db.query(Ticket).filter(Ticket.created_at >= today_start).count()
        
        announce = request.session.pop("announce", None)        # one-shot: gone after this page load
        announce_clips = build_announcement_clips(announce) if announce else []      
              
        message = request.session.pop("flash", None)
        response =  templates.TemplateResponse(request, "queue_management.html", {
            
            "total_today": total_today,
            "tickets": tickets,
            "services": services,
            "all_active": all_active,
            "waiting_count": waiting_count,
            "called_count": called_count,
            "skipped_count": skipped_count,
            "current_ticket": current_ticket,
            "upcomming": upcomming,
            "announcement_clips": announce_clips,
            "message": message
        })
        
        return response


# LIVE PANEL
@app.post("/call-next/{ticket_id}", dependencies=[Depends(login_required)])
def call_next(request: Request, ticket_id: int):
    upcoming = get_incoming_order(limit=1)
    if not upcoming:
        flash(request, False, "Nobody is waiting.")
        return RedirectResponse(url="/queue_management", status_code=303)
    
    target = upcoming[0]
    
    if target.id != ticket_id:       # the queue changed since the page was drawn
        flash(request, False, f"The queue changed. The next visitor is now {target.ticket_number}. "
                              "Please check and press Call next again.")
        return RedirectResponse(url="/queue_management", status_code=303)
    
    with SessionLocal() as db:
        claimed = db.execute(_CLAIM, {"id": target.id, "from_status": "waiting", "now": _stamp(), "today_start": _today_start_str()}).rowcount == 1
        db.commit()
    if not claimed:
        flash(request, False, "Someone is already at the counter. Finish or skip that visitor first.")# someone else got there first
        return RedirectResponse(url="/queue_management", status_code=303)
    
    request.session["announce"] = target.ticket_number      # played once (see #7)
    log_action("Queue", "Ticket Called", f"Called {target.ticket_number}", request)
    notify_display_update()
    return RedirectResponse(url="/queue_management", status_code=303)


def finish_ticket(ticket_id: int, final_status: str, allowed_statuses: tuple):
    """Atomically archive an eligible ticket exactly once."""
    
    db = SessionLocal()

    try:
        # Serialize this database write sequence.
        db.execute(text("BEGIN IMMEDIATE"))
        
        ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if ticket is None or ticket.status not in allowed_statuses:
            db.rollback()
            return None

        # Copy everything needed BEFORE deleting: the object is unusable once its row is gone
        number = ticket.ticket_number
        visit = Visit(
            citizen_name=ticket.citizen_name,
            lane=ticket.lane,
            ticket_number=number,
            final_status=final_status,
            service_id=ticket.service_id,
            created_at=ticket.created_at,
            called_at=ticket.called_at,
            completed_at=datetime.now(),
        )

        # Delete FIRST and check how many rows it removed. If two requests arrive together, only
        # one of them really removes the row, and only that one saves a Visit.
        removed = db.query(Ticket).filter(Ticket.id == ticket_id, Ticket.status.in_(allowed_statuses)).delete(synchronize_session=False)
        if removed != 1:
            db.rollback()
            return None

        db.add(visit)
        db.commit()   # the delete and the new Visit are saved together, or not at all
        return number
    
    except Exception:
        db.rollback()
        raise

    finally:
        db.close()
    
def get_incoming_order(limit: int = 3):
    """Simulate the 2:1 priority:regular call order without actually calling anyone."""
    
    start = datetime.combine(date.today(), time.min)
    with SessionLocal() as db:
        streak = current_priority_streak(db)
        waiting = db.query(Ticket).filter(Ticket.status == "waiting", Ticket.created_at >= start) \
                    .order_by(Ticket.created_at)
        waiting_priority = waiting.filter(Ticket.lane == "P").all()
        waiting_regular = waiting.filter(Ticket.lane == "R").all()

    upcoming, p_index, r_index = [], 0, 0
    while len(upcoming) < limit and (p_index < len(waiting_priority) or r_index < len(waiting_regular)):
        if streak < 2 and p_index < len(waiting_priority):
            upcoming.append(waiting_priority[p_index]); p_index += 1; streak += 1
        elif r_index < len(waiting_regular):
            upcoming.append(waiting_regular[r_index]); r_index += 1; streak = 0
        else:
            upcoming.append(waiting_priority[p_index]); p_index += 1; streak += 1
    return upcoming

def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")   # same format SQLAlchemy stores


@app.post("/skip/{ticket_id}", dependencies=[Depends(login_required)])
def skip_ticket(request: Request, ticket_id: int):
    
    with SessionLocal() as db:
        changed = db.query(Ticket).filter(Ticket.id == ticket_id, Ticket.status == "called") \
                    .update({"status": "skipped"}, synchronize_session=False)
        number = db.query(Ticket.ticket_number).filter(Ticket.id == ticket_id).scalar() if changed == 1 else None
        db.commit()
    if number:
        log_action("Queue", "Ticket Skipped", f"Skipped {number}", request)
        notify_display_update()                      # from #6
    return RedirectResponse(url="/queue_management", status_code=303)


@app.post("/recall/{ticket_id}", dependencies=[Depends(login_required)])
def recall_ticket(request: Request, ticket_id: int):
    with SessionLocal() as db:
        claimed = db.execute(_CLAIM, {"id": ticket_id, "from_status": "skipped", "now": _stamp(), "today_start": _today_start_str()}).rowcount == 1
        db.commit()
        number = db.query(Ticket.ticket_number).filter(Ticket.id == ticket_id).scalar() if claimed else None
    if number is None:
        return RedirectResponse(url="/queue_management", status_code=303)
    request.session["announce"] = number
    log_action("Queue", "Ticket Recalled", f"Recalled skipped ticket {number}", request)
    notify_display_update()
    return RedirectResponse(url="/queue_management", status_code=303)

@app.post("/complete/{ticket_id}", dependencies=[Depends(login_required)])
def complete_ticket(request: Request, ticket_id: int):
    # Only the visitor at the counter can be completed
    number = finish_ticket(ticket_id, "completed", ("called",))
    if number:
        log_action("Queue", "Ticket Completed", f"Marked {number} as completed", request)
        notify_display_update()
    return RedirectResponse(url="/queue_management", status_code=303)


@app.post("/no-show/{ticket_id}", dependencies=[Depends(login_required)])
def no_show_ticket(request: Request, ticket_id: int):
    # The visitor at the counter, or one in the skipped list
    number = finish_ticket(ticket_id, "no_show", ("called", "skipped"))
    if number:
        log_action("Queue", "Ticket No-Show", f"Marked {number} as no-show", request)
        notify_display_update()
    return RedirectResponse(url="/queue_management", status_code=303)

@app.post("/queue/clear", dependencies=[Depends(login_required)])
def clear_queue(request: Request):
    """End-of-day cleanup: every unserved ticket becomes a no-show in attendance history.

    Ticket numbers do NOT restart: they are counted from today's tickets AND visits.
    """
    db = SessionLocal()
    counts = Counter()
    try:
        db.execute(text("BEGIN IMMEDIATE"))
        
        tickets = db.query(Ticket).filter(Ticket.status.in_(["waiting", "called", "skipped"])).all()
        now = datetime.now()
        for t in tickets:
            counts[t.status] += 1
            db.add(Visit(
                citizen_name=t.citizen_name, lane=t.lane, ticket_number=t.ticket_number,
                final_status="no_show", service_id=t.service_id,
                created_at=t.created_at, called_at=t.called_at, completed_at=now,
            ))
            db.delete(t)
        db.commit()
    finally:
        db.close()

    total = sum(counts.values())
    if total:
        log_action("Queue", "Queue Cleared",
                   f"Cleared {total} unserved ticket(s) ({counts['waiting']} waiting, {counts['called']} called, "
                   f"{counts['skipped']} skipped); all saved as no-show", request)
        notify_display_update()
    return RedirectResponse(url="/queue_management", status_code=303)

@app.get("/attendance", dependencies=[Depends(login_required)])
def show_attendance(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    lane: str = "",
    service_id: str = "",
    q: str = "",
    page: str = "1",
):  
    service_id_int = parse_optional_int(service_id)
    
    if service_id and parse_optional_int(service_id) is None:
        return RedirectResponse(str(request.url.remove_query_params("service_id")), status_code=303)
    
    db = SessionLocal()
    try:
        # Single day filter - defaults to today when `date` is empty.
        # (resolve_date_range takes a from/to pair; passing `date` for both
        # gives one day, exactly like the print route's single-day case.)
        d_from, d_to = resolve_date_range(date_from, date_to)

        base = apply_visit_filters(db.query(Visit), d_from, d_to, lane, service_id_int, q)

        total_records = base.count()                                   # count BEFORE offset/limit
        page = parse_optional_int(page) or 1
        total_pages = max(1, math.ceil(total_records / PAGE_SIZE))
        page = max(1, min(page, total_pages))                           # clamp a stale ?page= from the URL

        visits = (
            base.order_by(Visit.created_at.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )

        start_record = (page - 1) * PAGE_SIZE + 1 if total_records else 0
        end_record = min(page * PAGE_SIZE, total_records)

        services = db.query(Service).all()

        response = templates.TemplateResponse(request, "attendance.html", {
            "visits": visits,
            "services": services,
            
            "date_from": d_from.isoformat(),   # feeds the "From Date" input
            "date_to": d_to.isoformat(),
            
            "lane": lane,
            "service_id": service_id_int,
            "search": q,
            "page": page,
            "total_pages": total_pages,
            "total_records": total_records,
            "start_record": start_record,
            "end_record": end_record,
        })
    finally:
        db.close()
    return response

def resolve_date_range(date_from: str, date_to: str):
    """
    The date filter always has a value - there is no "all dates".
    - nothing given         -> today
    - only date_from given  -> that single day
    - both given            -> that range (swapped if entered backwards)
    """
    d_from = parse_date(date_from) or date.today()
    d_to = parse_date(date_to) or d_from
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    return d_from, d_to

def range_label(d_from, d_to):
    fmt = lambda d: d.strftime("%b %d, %Y")
    if d_from == d_to:
        return fmt(d_from)                     # e.g. "Oct 24, 2026"
    return f"{fmt(d_from)} to {fmt(d_to)}"     # e.g. "Oct 24, 2026 to Oct 30, 2026"

def apply_visit_filters(query, d_from, d_to, lane, service_id, q):
    """Use this in BOTH /attendance and /attendance/print so they never disagree."""
    start_dt = datetime.combine(d_from, time.min)   # d_from 00:00:00.000000
    end_dt = datetime.combine(d_to, time.max)        # d_to   23:59:59.999999
    query = query.filter(Visit.created_at >= start_dt, Visit.created_at <= end_dt)

    if lane:
        query = query.filter(Visit.lane == lane)
    if service_id:
        query = query.filter(Visit.service_id == service_id)
    if q:
        like = like_pattern(q)
        query = query.filter(Visit.ticket_number.ilike(like, escape="\\")
                             | Visit.citizen_name.ilike(like, escape="\\"))
    return query

def like_pattern(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"

@app.get("/attendance/print", dependencies=[Depends(login_required)])
def attendance_print(request: Request, date_from: str = "", date_to: str = "", lane: str = "", service_id: str = "", q: str = ""):
    
    service_id_int = parse_optional_int(service_id)
    if service_id and parse_optional_int(service_id) is None:
        return RedirectResponse(str(request.url.remove_query_params("service_id")), status_code=303)
    
    db = SessionLocal()
    
    try:
        d_from, d_to = resolve_date_range(date_from, date_to)
        
        base = apply_visit_filters(db.query(Visit), d_from, d_to, lane, service_id_int, q)
        
        completed = base.filter(Visit.final_status == "completed").count()
        no_show = base.filter(Visit.final_status == "no_show").count()
        stats = {
            "issued": completed + no_show,
            "completed": completed,
            "no_show": no_show
        }
        
        rows = base.order_by(Visit.created_at).all()
        

        # Human-readable filter summary for the toolbar
        service = db.get(Service, service_id_int) if service_id_int else None
        
        parts = [
            range_label(d_from, d_to),
            lane.title() if lane else "All Types",
            service.service_name if service else "All Services",
        ]
        if q:
            parts.append(f'Search: "{q}"')
            
        log_action_throttled(request, "Database", "Attendance Printed", f"Opened the attendance print view ({', '.join(parts)})")
        return templates.TemplateResponse(request,"print_attendance.html", {
            
            "stats": stats,
            "rows": rows,
            "total_rows": len(rows),
            "filters_text": ", ".join(parts),
            "range_text": range_label(d_from, d_to),
            "barangay_name": "Barangay 4, San Mateo, Isabela",
            "generated_at": datetime.now().strftime("%m/%d/%Y %I:%M %p"),
        },
    )
    finally:
        db.close()
        
_CSV_DANGEROUS = ("=", "+", "-", "@", "\t", "\r")
def csv_safe(value) -> str:
    """Stop Excel from running a cell that starts with = + - @ as a formula."""
    s = "" if value is None else str(value)
    return "'" + s if s.startswith(_CSV_DANGEROUS) else s


@app.get("/attendance/export", dependencies=[Depends(login_required)])
def attendance_export(request: Request, date_from: str = "", date_to: str = "", lane: str = "", service_id: str = "", q: str = ""):
    """Download the attendance table (same filters as the page) as a CSV file."""
    service_id_int = parse_optional_int(service_id)
    d_from, d_to = resolve_date_range(date_from, date_to)

    if service_id and parse_optional_int(service_id) is None:
        return RedirectResponse(str(request.url.remove_query_params("service_id")), status_code=303)
    db = SessionLocal()
    try:
        rows = apply_visit_filters(db.query(Visit), d_from, d_to, lane, service_id_int, q) \
            .order_by(Visit.created_at).all()

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["Date", "Ticket", "Client name", "Type", "Service", "Time in", "Time out", "Status"])
        for v in rows:
            writer.writerow([
                v.created_at.strftime("%m/%d/%Y"),
                csv_safe(v.ticket_number),
                csv_safe(v.citizen_name),
                "Priority" if v.lane == "P" else "Regular",
                csv_safe(v.service.service_name if v.service else "Removed service"),
                v.called_at.strftime("%I:%M %p") if v.called_at else "",
                v.completed_at.strftime("%I:%M %p") if v.completed_at else "",
                (v.final_status or "").replace("_", " ").title(),
            ])
    finally:
        db.close()

    log_action("Database", "CSV Export", f"Exported {len(rows)} attendance record(s) for {range_label(d_from, d_to)}", request)
    return Response(
        content="\ufeff" + buffer.getvalue(),   # BOM so Excel reads UTF-8 names correctly
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="attendance_{d_from}_to_{d_to}.csv"'},
    )
    
    
# SERVICE
def service_name_error(db, name: str, exclude_id: int | None = None) -> str | None:
    if not name:
        return "Service name is required."
    q = db.query(Service).filter(func.lower(Service.service_name) == name.lower())
    if exclude_id:
        q = q.filter(Service.id != exclude_id)
    return f'A service named "{name}" already exists.' if q.first() else None

@app.get("/services", dependencies=[Depends(login_required)])
def list_services(request: Request):
    message = request.session.pop("flash", None)
    
    with SessionLocal() as db:
        services = db.query(Service).all()
        today_start = datetime.combine(date.today(), datetime.min.time())
                    
        total_today = db.query(Ticket).filter(Ticket.created_at >= today_start).count()
        return templates.TemplateResponse(request, "services.html", {
            
            "services": services,
            "total_today": total_today,
            "message": message
            
        })

@app.post("/services/add", dependencies=[Depends(login_required)])
def add_services(request: Request, service_name: str = Form(...), service_status: StatusEnum = Form(alias="service_status")):
    name = clean_text(service_name, 80)
    with SessionLocal() as db:
        error = service_name_error(db, name)
        if error:
            flash(request, False, error)
            return RedirectResponse(url="/services", status_code=303)
        db.add(Service(service_name=name, service_status=service_status.value))
        db.commit()
    log_action("Service", "Service Added", f"Added service: {name} ({service_status.value})", request)
    return RedirectResponse(url="/services", status_code=303)

@app.post("/services/edit/{service_id}", dependencies=[Depends(login_required)])
def edit_service(request: Request, service_id: int, service_name: str = Form(...), service_status: StatusEnum = Form(alias="service_status")):
    changes = []
    name = clean_text(service_name, 80)
    with SessionLocal() as db:
        service = db.get(Service, service_id)
        error = service_name_error(db, name, exclude_id=service_id)
        
        if error:
            flash(request, False, error)
            return RedirectResponse(url="/services", status_code=303)

        if service:
            if service.service_name != name:
                changes.append(f'renamed from "{service.service_name}"')
            if service.service_status != service_status.value:
                changes.append(f"status {service.service_status} -> {service_status.value}")
            service.service_name = name
            service.service_status = service_status.value
            db.commit()
    if changes:   # saving the form without changing anything is not worth a log row
        log_action("Service", "Service Modified", f'Modified service "{name}": ' + "; ".join(changes), request)

    return RedirectResponse(url="/services", status_code=303)

@app.post("/services/delete/{service_id}", dependencies=[Depends(login_required)])
def delete_service(request: Request, service_id: int):
    db = SessionLocal()
    log_entry = None
    try:
        service = db.query(Service).filter(Service.id == service_id).first()
        if service:
            name = service.service_name
            has_history = (
                db.query(Ticket).filter(Ticket.service_id == service_id).first() is not None
                or db.query(Visit).filter(Visit.service_id == service_id).first() is not None
            )
            if has_history:
                # Old tickets and visits still point at this service, so keep the row
                service.service_status = "Inactive"
                log_entry = ("Service Deactivated", f'Deleted service "{name}", but it has ticket history, so it was set to Inactive instead')
            else:
                try:
                    db.delete(service)
                    db.commit()
                    log_entry = ("Service Deleted", f"Deleted service: {name}")
                except IntegrityError:         # a ticket appeared a moment ago: keep the row
                    db.rollback()
                    db.get(Service, service_id).service_status = "Inactive"
                    db.commit()
                    log_entry = ("Service Deactivated", f'"{name}" gained ticket history, so it was set to Inactive')
            db.commit()
    finally:
        db.close()
    if log_entry:
        log_action("Service", log_entry[0], log_entry[1], request)
    return RedirectResponse(url="/services", status_code=303)

# AUDIT LOGS
@app.get("/audit_logs", dependencies=[Depends(login_required)])
def show_audit_logs(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    category: str = "",
    q: str = "",
    page: str = "1",
):
    # Same rule as Attendance: the date filter always has a value (today by default)
    d_from, d_to = resolve_date_range(date_from, date_to)
    if category not in LOG_CATEGORIES:
        category = ""

    db = SessionLocal()
    try:
        query = db.query(AdminLog).filter(AdminLog.created_at >= datetime.combine(d_from, time.min),
                                          AdminLog.created_at <= datetime.combine(d_to, time.max))

        if category:
            query = query.filter(AdminLog.category == category)
        if q:
            like = like_pattern(q)
            query = query.filter(AdminLog.admin_username.ilike(like, escape="\\")
                                 | AdminLog.action.ilike(like, escape="\\")
                                 | AdminLog.description.ilike(like, escape="\\"))

        total_records = query.count()
        page = parse_optional_int(page) or 1
        total_pages = max(1, math.ceil(total_records / PAGE_SIZE))
        page = max(1, min(page, total_pages))

        logs = (
            query.order_by(AdminLog.created_at.desc(), AdminLog.id.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )

        response = templates.TemplateResponse(request, "audit_logs.html", {
            "logs": logs,
            "categories": LOG_CATEGORIES,
            "date_from": d_from.isoformat(),
            "date_to": d_to.isoformat(),
            "category": category,
            "search": q,
            "page": page,
            "total_pages": total_pages,
            "total_records": total_records,
            "start_record": (page - 1) * PAGE_SIZE + 1 if total_records else 0,
            "end_record": min(page * PAGE_SIZE, total_records),
        })
    finally:
        db.close()
    return response


# MAINTENANCE (backup + purge)
@app.get("/maintenance", dependencies=[Depends(login_required)])
def show_maintenance(request: Request):
    message = request.session.pop("flash", None)

    db_file = Path(data_path("barangay_system.db"))
    db_size_mb = round(db_file.stat().st_size / 1048576, 2) if db_file.exists() else 0

    db = SessionLocal()
    try:
        visit_count = db.query(Visit).count()
        oldest_visit = db.query(func.min(Visit.created_at)).scalar()
    finally:
        db.close()

    return templates.TemplateResponse(request, "maintenance.html", {
        "message": message,
        "db_size_mb": db_size_mb,
        "visit_count": visit_count,
        "oldest_visit": oldest_visit,
        "default_backup_dir": str(BACKUP_DIR_DEFAULT),
        "purge_min_days": PURGE_MIN_AGE_DAYS,
        "latest_cutoff": (date.today() - timedelta(days=PURGE_MIN_AGE_DAYS)).isoformat(),
        "printers": list_windows_printers(),
        "printer": load_printer_settings(),
        "dummy_printer": USE_DUMMY_PRINTER,
    })


@app.post("/maintenance/backup", dependencies=[Depends(login_required)])
def run_backup(request: Request, folder: str = Form("")):
    """Copy the live database to a timestamped file using SQLite's own backup API.

    (A plain file copy of a database that is being written to can produce a damaged copy.)
    """
    folder = folder.strip().strip('"')
    target_dir = Path(folder) if folder else BACKUP_DIR_DEFAULT
    try:
        if not folder:
            target_dir.mkdir(parents=True, exist_ok=True)
        elif not target_dir.is_absolute() or not target_dir.is_dir():
            raise FileNotFoundError(f"folder not found (use a full path such as D:\\Backups): {target_dir}")

        target = create_backup_file(target_dir, "barangay_system_")   # temp file + integrity check
    
    except Exception as e:
        flash(request, False, f"Backup failed: {e}")
        log_action("Database", "Backup Failed", f"Backup to {target_dir} failed: {e}", request)
    else:
        flash(request, True, f"Backup saved: {target}")
        log_action("Database", "Backup", f"Database backup saved to {target}", request)
    return RedirectResponse(url="/maintenance", status_code=303)

@app.post("/maintenance/printer/save", dependencies=[Depends(login_required)])
def save_printer(request: Request, printer_name: str = Form(""), paper_mm: str = Form("58")):
    name = clean_text(printer_name, 200)
    if paper_mm not in PAPER_COLUMNS:
        flash(request, False, "Choose 58 mm or 80 mm paper.")
    elif name and not USE_DUMMY_PRINTER and name not in list_windows_printers():
        flash(request, False, "That printer is not installed on this computer.")
    else:
        try:
            save_printer_settings(name, paper_mm)
        except OSError as e:
            log.exception("Saving printer settings failed")
            flash(request, False, f"Could not save the printer settings: {e}")
        else:
            flash(request, True, f'Receipt printer saved: {name or "none"} ({paper_mm} mm paper).')
            log_action("Settings", "Printer Changed", f'Receipt printer set to "{name or "none"}" ({paper_mm} mm paper)', request)
    return RedirectResponse(url="/maintenance", status_code=303)


@app.post("/maintenance/printer/test", dependencies=[Depends(login_required)])
def test_printer(request: Request):
    settings = load_printer_settings()
    try:
        deliver_to_printer(build_test_slip(PAPER_COLUMNS[settings["paper_mm"]]), settings)
    except Exception as e:
        log.exception("Test print failed")
        flash(request, False, f"Test print failed: {e}")
        log_action("Settings", "Print Error", f"Test print failed: {e}", request)
    else:
        flash(request, True, "Test slip sent to the printer. Check that it came out correctly.")
        log_action("Settings", "Test Print", "Printed a test slip", request)
    return RedirectResponse(url="/maintenance", status_code=303)

@app.post("/maintenance/purge", dependencies=[Depends(login_required)])
def purge_old_records(request: Request, cutoff: str = Form(...)):
    """Delete attendance history (visits) from before the cutoff date.

    Only the visits table is touched: never the live queue, services, accounts or the audit log.
    """
    cutoff_date = parse_date(cutoff)
    latest_allowed = date.today() - timedelta(days=PURGE_MIN_AGE_DAYS)

    if cutoff_date is None:
        flash(request, False, "Pick a valid date.")
    elif cutoff_date > latest_allowed:
        flash(request, False, f"For safety, only records older than {PURGE_MIN_AGE_DAYS} days can be purged "
                              f"(latest allowed date: {latest_allowed.strftime('%b %d, %Y')}).")
    else:
        try:
            BACKUP_DIR_DEFAULT.mkdir(parents=True, exist_ok=True)
            create_backup_file(BACKUP_DIR_DEFAULT, "prepurge_")
        except Exception as e:
            flash(request, False, f"Purge cancelled: a safety backup could not be made first ({e}).")
            return RedirectResponse(url="/maintenance", status_code=303)
        
        db = SessionLocal()
        try:
            deleted = db.query(Visit).filter(
                Visit.created_at < datetime.combine(cutoff_date, time.min)
            ).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

        if deleted:
            try:   # hand the freed space back to the disk
                with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                    conn.execute(text("VACUUM"))
            except Exception:
                log.exception("VACUUM failed")
            log_action("Database", "Data Purge",
                       f"Purged {deleted} attendance record(s) from before {cutoff_date.strftime('%b %d, %Y')}", request)
            flash(request, True, f"Purged {deleted} attendance record(s) from before {cutoff_date.strftime('%b %d, %Y')}.")
        else:
            flash(request, True, f"No attendance records exist from before {cutoff_date.strftime('%b %d, %Y')}. Nothing was deleted.")
    return RedirectResponse(url="/maintenance", status_code=303)

# Public display page: ticket numbers only, never names

# ABOUT
@app.get("/about", dependencies=[Depends(login_required)])
def show_about(request: Request):
    return templates.TemplateResponse(request, "about.html", {})


@app.get("/display")
def display_page(request: Request):
    return templates.TemplateResponse(request, "display.html", {})

@app.get("/display/data")
def display_data():
    start = datetime.combine(date.today(), time.min)
    with SessionLocal() as db:
        current = db.query(Ticket).filter(Ticket.status == "called", Ticket.created_at >= start) \
                    .order_by(Ticket.called_at.desc()).first()
        now_serving = current.ticket_number if current else None
    return {"now_serving": now_serving, "upcoming": [t.ticket_number for t in get_incoming_order(limit=3)]}