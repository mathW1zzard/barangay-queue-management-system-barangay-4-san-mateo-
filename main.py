from fastapi import FastAPI, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi import Request 
from fastapi import Form
from fastapi import BackgroundTasks
from fastapi import Depends
from fastapi.responses import RedirectResponse, Response
from database import SessionLocal, Ticket, Service, Visit, Admin, AdminLog, engine
from paths import resource_path, data_path
from datetime import datetime, date, time, timedelta
from starlette.middleware.sessions import SessionMiddleware
from enum import Enum
from fastapi import WebSocket, WebSocketDisconnect
from escpos.printer import Dummy, Win32Raw
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

# For authentication(recovery key)
import secrets
import string

app = FastAPI()
def load_secret_key() -> str:
    key_file = Path(data_path("session_secret.key"))
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32))
    return key_file.read_text().strip()

app.add_middleware(SessionMiddleware, secret_key=load_secret_key())

templates = Jinja2Templates(directory=resource_path("templates"))
priority_streak = 0
PAGE_SIZE = 15

# ADMIN LOGS
LOG_CATEGORIES = ["Queue", "Service", "Settings", "Security", "Database"]
NOT_SIGNED_IN = "(not signed in)" # who gets blamed for failed logins

# SESSION TRACKING (so the audit log can record a session that ended by closing the app)
active_admin_id = None
SESSION_EVENTS = ("Login", "Logout", "Session Closed", "Session Interrupted")

# MAINTENANCE
BACKUP_DIR_DEFAULT = Path(data_path("backups"))
PURGE_MIN_AGE_DAYS = 30 # safety: records newer than this can never be purged

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
    except Exception as e:
        print(f"Audit log failed: {e}")

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
        
    except Exception as e:
        print(f"Startup audit check failed: {e}")
  
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
        print(f"Closed audit log failed: {e}")
          
def parse_date(value: str):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


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
        self.active_connections: list[WebSocket] = []
        
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        
    def disconnect(self, websocket: WebSocket):
            self.active_connections.remove(websocket)
            
    async def broadcast(self, message: str):
        for connection in self.active_connections:
            await connection.send_text(message)
manager = ConnectionManager()

@app.websocket("/ws/display")
async def websocket_display(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

def notify_display_update():
    asyncio.run(manager.broadcast("update"))
    
# ANNOUNCEMENT AUDIO
def build_announcement_clips(ticket_number: str):
    """Convert a ticket number such as P-047 into a list of reusable audio clips."""

    letter, digits = ticket_number.split("-")

    clips = [
        "ticket_number",
        f"letters/{letter}"
    ]
    clips += [
        f"digits/{digit}"
        for digit in digits
    ]

    clips.append("proceed_to_counter")

    return clips

       
# True  = the slip prints by itself when a ticket is registered (the confirmation page then
#         offers "Print again").
# False = nothing prints until "Print ticket slip" is pressed on the confirmation page.
AUTO_PRINT_ON_REGISTER = True

def get_printer():
    """The one place that decides which printer is used.

    Dummy() prints nothing; it only keeps the ESC/POS bytes (see last_ticket_printed.txt).
    For the real USB thermal printer, replace the line below with, for example:
        from escpos.printer import Usb
        return Usb(0x04b8, 0x0e15)   # your printer's vendor ID and product ID
    """
    return Dummy()

def print_ticket(ticket_number: str, citizen_name: str, service_name: str, lane: str, created_at: datetime) -> bool:
    """Send the queue slip to the printer. Returns True if it was sent, False if it failed.

    The lines match the slip on the confirmation page, so what is printed is what was confirmed.
    """
    try:
        printer = get_printer()
        rule = "-" * 32 + "\n"

        printer.set(align="center", bold=True, width=1, height=1)
        printer.text(f"{BARANGAY_NAME.upper()}\n")
        printer.set(align="center", bold=False, width=1, height=1)
        printer.text("Official Queue Slip\n")
        printer.text(rule)

        printer.set(align="center", bold=True, width=2, height=2)
        printer.text(f"{ticket_number}\n")
        printer.set(align="center", bold=True, width=1, height=1)
        printer.text(("PRIORITY LANE" if lane == "P" else "REGULAR LANE") + "\n")
        printer.text(rule)

        printer.set(align="left", bold=False, width=1, height=1)
        printer.text(f"Client : {citizen_name}\n")
        printer.text(f"Service: {service_name}\n")
        printer.text(f"Date   : {created_at.strftime('%b %d, %Y %I:%M %p')}\n")
        printer.text(rule)

        printer.set(align="center", bold=False, width=1, height=1)
        printer.text("Please wait for your number\nto be called. Thank you!\n\n")
        printer.cut()

        # Dummy doesn't print; save what WOULD be printed so it can be inspected.
        if isinstance(printer, Dummy):
            with open(data_path("last_ticket_printed.txt"), "wb") as f:
                f.write(printer.output)
            print("Ticket sent to (dummy) printer - check last_ticket_printed.txt")
        return True
    except Exception as e:
        print(f"Ticket printing failed: {e}")
        log_action("Settings", "Print Error", f"Ticket slip {ticket_number} failed to print: {e}", username="system")
        return False


@app.get("/")
def read_root():
    db = SessionLocal()
    user_exists = db.query(Admin).first()
    db.close()
    if not user_exists:
        return RedirectResponse(url="/setup", status_code=303)
    return RedirectResponse(url="/login", status_code=303)

@app.get("/setup")
def show_setup(request: Request):
    db = SessionLocal()
    existing_admin = db.query(Admin).first()
    db.close()

    if existing_admin:
        return RedirectResponse(url="/login", status_code=303)

    return templates.TemplateResponse(request, "setup1.html", {})


@app.post("/setup")
def process_setup(request: Request, username: str = Form(...), password: str = Form(...), confirm_password: str = Form(...)):
    db = SessionLocal()
    try:
        # 1. Setup already done -> never create a second admin
        if db.query(Admin).first() is not None:
            return RedirectResponse(url="/login", status_code=303)
        
        # 2. Validate input
        if password != confirm_password:
            return templates.TemplateResponse(request, "setup1.html", {"error": "Passwords do not match"})

        if len(password) < 8:
            return templates.TemplateResponse(request, "setup1.html", {"error": "Password must be at least 8 characters"})

        # 3. Create the admin
        recovery_key = generate_recovery_key()
        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        recovery_key_hash = bcrypt.hashpw(recovery_key.encode(), bcrypt.gensalt()).decode()
            
        new_admin = Admin(username=username, password_hash=password_hash, recovery_key_hash=recovery_key_hash)
        db.add(new_admin)
        db.commit()
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
    db = SessionLocal()
    admin = db.query(Admin).filter(Admin.username == username).first()
    db.close()

    if not admin or not bcrypt.checkpw(password.encode(), admin.password_hash.encode()):
        log_action("Security", "Login Failed", f'Failed login attempt for username "{username[:50]}"', username=NOT_SIGNED_IN)
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid username or password"})

    global active_admin_id
    request.session["admin_id"] = admin.id
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
    db = SessionLocal()
    admin = db.query(Admin).filter(Admin.username == username).first()

    error = None
    if not admin or not admin.recovery_key_hash or not bcrypt.checkpw(recovery_key.encode(), admin.recovery_key_hash.encode()):
        error = "Invalid username or recovery key"
    elif new_password != confirm_password:
        error = "Passwords do not match"
    elif len(new_password) < 8:
        error = "Password must be at least 8 characters"

    if error:
        db.close()
        if error.startswith("Invalid username"):
            log_action("Security", "Recovery Failed", f'Failed password reset attempt for username "{username[:50]}"', username=NOT_SIGNED_IN)
        return templates.TemplateResponse(request, "forgot_password.html", {"error": error})

    # Reset password AND issue a brand new recovery key (the old one was just used, so retire it)
    new_recovery_key = generate_recovery_key()
    admin.password_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    admin.recovery_key_hash = bcrypt.hashpw(new_recovery_key.encode(), bcrypt.gensalt()).decode()
    db.commit()
    db.close()

    log_action("Security", "Password Reset", "Password reset using the recovery key; a new recovery key was issued", username=username)
    return templates.TemplateResponse(request, "recovery_key_display.html", {"recovery_key": new_recovery_key})

@app.post("/logout")
def logout(request: Request):
    global active_admin_id
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
    """Use as dependencies=[Depends(login_required)] on every staff-only route."""
    if "admin_id" not in request.session:
        raise NotAuthenticated()
    global active_admin_id
    active_admin_id = request.session["admin_id"]


@app.get("/change-password", dependencies=[Depends(login_required)])
def show_change_password(request: Request):
    return templates.TemplateResponse(request, "change_password.html", {})


@app.post("/change-password", dependencies=[Depends(login_required)])
def process_change_password(request: Request, current_password: str = Form(...), new_password: str = Form(...), confirm_password: str = Form(...)):
    db = SessionLocal()
    try:
        admin = db.query(Admin).filter(Admin.id == request.session["admin_id"]).first()
        if not admin:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)

        error = None
        if not bcrypt.checkpw(current_password.encode(), admin.password_hash.encode()):
            error = "Current password is incorrect"
        elif new_password != confirm_password:
            error = "New passwords do not match"
        elif len(new_password) < 8:
            error = "Password must be at least 8 characters"

        if error:
            if error == "Current password is incorrect":
                log_action("Security", "Password Change Failed", "Wrong current password entered", request)
            return templates.TemplateResponse(request, "change_password.html", {"error": error})

        admin.password_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
        db.commit()
    finally:
        db.close()
        
    log_action("Security", "Password Changed", "Changed their own password", request)
    return templates.TemplateResponse(request, "change_password.html", {"success": "Password updated."})

def generate_recovery_key():
    """Generates a key like BQS-123A-456B-789C"""
    chars = string.ascii_uppercase + string.digits
    groups = ["".join(secrets.choice(chars) for _ in range(4)) for _ in range(3)]
    return "BQS-" + "-".join(groups)


def get_current_admin(request: Request):
    admin_id = request.session.get("admin_id")
    if not admin_id:
        return None
    db = SessionLocal()
    admin = db.query(Admin).filter(Admin.id == admin_id).first()
    db.close()
    return admin

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
    db = SessionLocal()
    tickets = db.query(Ticket).all()
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
            "service_name": t.service.service_name,
            "status": t.status,
            "sort_time": t.called_at or t.created_at,
        })
    for v in visits_today:
        overview.append({
            "ticket_number": v.ticket_number,
            "citizen_name": v.citizen_name,
            "service_name": v.service.service_name,
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
        "tickets": tickets,
        "services": services,
        "service_volume": service_volume    
    })
    db.close()
    return response

@app.get("/client_registration", dependencies=[Depends(login_required)])
def show_registration(request: Request):
    db = SessionLocal()
    today_start = datetime.combine(date.today(), datetime.min.time())

    tickets = db.query(Ticket).filter(Ticket.created_at >= today_start) \
        .order_by(desc(Ticket.created_at)).all()
    services = db.query(Service).filter(Service.service_status == "Active").all()

    response = templates.TemplateResponse(request, "client_registration.html", {
        "tickets": tickets,
        "services": services,
        "total_today": len(tickets),
        "queue_info": queue_snapshot()
    })
    db.close()
    return response

def queue_snapshot() -> dict:
    """Numbers for the Live Queue panel on the registration page."""
    
    db = SessionLocal()
    
    try:
        today_start = datetime.combine(date.today(), datetime.min.time())
        
        current = db.query(Ticket).filter(Ticket.status == "called") \
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
def register_ticket(citizen_name: str =  Form(...), lane: str = Form(...), service_id: int = Form(...)):
    citizen_name = citizen_name.strip()
    if not citizen_name:
        raise HTTPException(status_code=400, detail="Client name is required.")
    if lane not in ("P", "R"):
        raise HTTPException(status_code=400, detail="Invalid lane.")

    db = SessionLocal()
    try:
        # Only active services can be queued for
        service = db.query(Service).filter(Service.id == service_id, Service.service_status == "Active").first()
        if service is None:
            raise HTTPException(status_code=400, detail="Selected service is unavailable.")
        service_name = service.service_name

        today_start = datetime.combine(date.today(), datetime.min.time())  # 00:00:00 today

        # Use the highest number already handed out today, not a row count.
        # A count would hand out a duplicate number once a ticket has been deleted.
        used_numbers = [
            row[0] for row in db.query(Ticket.ticket_number)
            .filter(Ticket.lane == lane, Ticket.created_at >= today_start).all()
        ] + [
            row[0] for row in db.query(Visit.ticket_number)
            .filter(Visit.lane == lane, Visit.created_at >= today_start).all()
        ]
        ticket_number = next_ticket_number(lane, used_numbers)
        created_at = datetime.now()

        ticket = Ticket(citizen_name=citizen_name, lane=lane, service_id=service_id,
                        ticket_number=ticket_number, created_at=created_at)
        db.add(ticket)
        db.commit()
        ticket_id = ticket.id
    finally:
        db.close()

    # Redirect instead of rendering here: a browser refresh then reloads the confirmation
    # page and cannot submit the form (and create a second ticket) again.
    url = f"/client_registration/confirmation/{ticket_id}"
    if AUTO_PRINT_ON_REGISTER:
        sent = print_ticket(ticket_number, citizen_name, service_name, lane, created_at)
        url += f"?printed={1 if sent else 0}"
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
                            ticket.service.service_name, ticket.lane, ticket.created_at)
    finally:
        db.close()
    log_action("Queue", "Slip Reprinted", f"Reprinted slip for {number}" + ("" if sent else " (printer error)"), request)
    return RedirectResponse(
        url=f"/client_registration/confirmation/{ticket_id}?printed={1 if sent else 0}",
        status_code=303,
    )


def next_ticket_number(lane: str, used_numbers):
    """Next number for a lane, e.g. R-004, given every number already used today."""
    highest = 0
    for number in used_numbers:
        try:
            highest = max(highest, int(str(number).split("-")[-1]))
        except ValueError:
            continue
    return f"{lane}-{highest + 1:03d}"


@app.post("/tickets/delete/{ticket_id}", dependencies=[Depends(login_required)])
def delete_ticket(request: Request, ticket_id: int):
    """Remove a ticket from the live queue. Nothing is written to attendance history."""
    db = SessionLocal()
    number = status = None
    try:
        ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if ticket:
            number, status = ticket.ticket_number, ticket.status
            db.delete(ticket)
            db.commit()
    finally:
        db.close()
    if number:
        log_action("Queue", "Ticket Deleted", f"Removed {number} (was {status}) from the live queue; not saved to attendance", request)
    return RedirectResponse(url="/client_registration", status_code=303)

# QUEUE MANAGEMENT
@app.get("/queue_management", dependencies=[Depends(login_required)])
def show_queue(request: Request, announce: str = ""):
    db = SessionLocal()
    tickets = db.query(Ticket).all()
    services = db.query(Service).all()
    upcomming = get_incoming_order(limit=3)
    active_statuses = ["waiting", "called", "skipped"]
    
    today_start = datetime.combine(date.today(), datetime.min.time())
    
    current_ticket = db.query(Ticket).filter(Ticket.status == "called") \
        .order_by(Ticket.called_at.desc()).first()

    all_active = db.query(Ticket).filter(Ticket.status.in_(active_statuses), Ticket.created_at >= today_start).count()
    waiting_count = db.query(Ticket).filter(Ticket.status == "waiting", Ticket.created_at >= today_start).count()
    called_count = db.query(Ticket).filter(Ticket.status == "called", Ticket.created_at >= today_start).count()
    skipped_count = db.query(Ticket).filter(Ticket.status == "skipped", Ticket.created_at >= today_start).count()
            
    total_today = db.query(Ticket).filter(Ticket.created_at >= today_start).count()
    
    announce_clips = []
    
    if announce:
        announce_clips = build_announcement_clips(announce)
        
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
        "announcement_clips": announce_clips
    })
    db.close()
    return response


# LIVE PANEL
@app.post("/call-next", dependencies=[Depends(login_required)])
def call_next(request: Request):
    global priority_streak
    db = SessionLocal()
    try:
        # Don't allow calling a new ticket while one is already being served
        if db.query(Ticket).filter(Ticket.status == "called").first():
            return RedirectResponse(url="/queue_management", status_code=303)

        # Same logic as the "Up Next" preview, so they can never disagree
        upcoming = get_incoming_order(limit=1)
        if not upcoming:
            return RedirectResponse(url="/queue_management", status_code=303)

        next_ticket = db.query(Ticket).filter(Ticket.id == upcoming[0].id).first()

        # Update the streak the same way the preview does
        if next_ticket.lane == "P":
            priority_streak += 1
        else:
            priority_streak = 0

        next_ticket.status = "called"
        next_ticket.called_at = datetime.now()
        number = next_ticket.ticket_number
        db.commit()
        
    finally:
        db.close()
    log_action("Queue", "Ticket Called", f"Called {number}", request)
    return RedirectResponse(url=f"/queue_management?announce={number}",status_code=303)


def archive_ticket(ticket_id : int, final_status : str):
    """Shared helper: mark a ticket finished and create its permanent Visit record."""
    db = SessionLocal()
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()


    if ticket:
        ticket.status = final_status
        ticket.completed_at = datetime.now()
        
        visit = Visit(
            citizen_name = ticket.citizen_name,
            lane = ticket.lane,
            ticket_number = ticket.ticket_number,
            final_status = final_status,
            service_id = ticket.service_id,
            created_at = ticket.created_at,
            called_at = ticket.called_at,
            completed_at = ticket.completed_at,
        )
        db.add(visit)
        db.commit()
        
    db.close()
    
def get_incoming_order(limit: int = 3):
    """Simulate the 2:1 priority:regular call order without actually calling anyone."""
    
    db = SessionLocal()
    
    waiting_priority = db.query(Ticket).filter(Ticket.status == "waiting", Ticket.lane == "P").order_by(Ticket.created_at).all()
    waiting_regular = db.query(Ticket).filter(Ticket.status == "waiting", Ticket.lane == "R").order_by(Ticket.created_at).all()
    db.close()
    
    upcoming = []
    streak = priority_streak # start from the CURRENT real streak, so the preview matches reality
    p_index = 0
    r_index = 0
    
    while len(upcoming) < limit and (p_index < len(waiting_priority) or r_index < len(waiting_regular)):
        
        if streak < 2 and p_index < len(waiting_priority):
            upcoming.append(waiting_priority[p_index])
            p_index += 1
            streak += 1
            
        elif r_index < len(waiting_regular):
            upcoming.append(waiting_regular[r_index])
            r_index += 1
            streak = 0
        elif p_index < len(waiting_priority):
            # regular ran out, fall back to priority
            upcoming.append(waiting_priority[p_index])
            p_index += 1
            streak += 1
        else:
            break
    return upcoming
    

@app.post("/skip/{ticket_id}", dependencies=[Depends(login_required)])
def skip_ticket(request: Request, ticket_id: int):
    db = SessionLocal()
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    number = None
    if ticket:
        ticket.status = "skipped"   # just a status change, NOT archived
        number = ticket.ticket_number
        db.commit()
    db.close()
    if number:
        log_action("Queue", "Ticket Skipped", f"Skipped {number}", request)
    return RedirectResponse(url="/queue_management", status_code=303)


@app.post("/recall/{ticket_id}", dependencies=[Depends(login_required)])
def recall_ticket(request: Request, ticket_id: int):
    db = SessionLocal()
    number = None
    try:
        if db.query(Ticket).filter(Ticket.status == "called").first():
            return RedirectResponse(url="/queue_management", status_code=303)
        
        ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if ticket and ticket.status == "skipped":
            ticket.status = "called"
            ticket.called_at = datetime.now()  
            number = ticket.ticket_number
            db.commit()
            
    finally:
        db.close()
        
    if number is None:   # nothing was recalled (ticket missing or not skipped)
        return RedirectResponse(url="/queue_management", status_code=303)

    log_action("Queue", "Ticket Recalled", f"Recalled skipped ticket {number}", request)
    return RedirectResponse(url=f"/queue_management?announce={number}", status_code=303)

@app.post("/complete/{ticket_id}", dependencies=[Depends(login_required)])
def complete_ticket(request: Request, ticket_id: int):
    archive_ticket(ticket_id, "completed")
    db = SessionLocal()
    
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    number = None
    
    if ticket:
        number = ticket.ticket_number
        db.delete(ticket)
        db.commit()
    db.close()
    if number:
        log_action("Queue", "Ticket Completed", f"Marked {number} as completed", request)
    return RedirectResponse(url="/queue_management", status_code=303)


@app.post("/no-show/{ticket_id}", dependencies=[Depends(login_required)])
def no_show_ticket(request: Request, ticket_id: int):
    archive_ticket(ticket_id, "no_show")   # reuses your existing archive helper - permanent
    db = SessionLocal()
        
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    number = None
        
    if ticket:
        number = ticket.ticket_number
        db.delete(ticket)
        db.commit()
    db.close()
    if number:
        log_action("Queue", "Ticket No-Show", f"Marked {number} as no-show", request)
    return RedirectResponse(url="/queue_management", status_code=303)

@app.post("/queue/clear", dependencies=[Depends(login_required)])
def clear_queue(request: Request):
    """End-of-day cleanup: every unserved ticket becomes a no-show in attendance history.

    Ticket numbers do NOT restart: they are counted from today's tickets AND visits.
    """
    global priority_streak
    db = SessionLocal()
    counts = Counter()
    try:
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
        priority_streak = 0
        log_action("Queue", "Queue Cleared",
                   f"Cleared {total} unserved ticket(s) ({counts['waiting']} waiting, {counts['called']} called, "
                   f"{counts['skipped']} skipped); all saved as no-show", request)
    return RedirectResponse(url="/queue_management", status_code=303)

@app.get("/attendance", dependencies=[Depends(login_required)])
def show_attendance(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    lane: str = "",
    service_id: str = "",
    q: str = "",
    page: int = 1,
):  
    service_id_int = int(service_id) if service_id else None
    db = SessionLocal()
    try:
        # Single day filter - defaults to today when `date` is empty.
        # (resolve_date_range takes a from/to pair; passing `date` for both
        # gives one day, exactly like the print route's single-day case.)
        d_from, d_to = resolve_date_range(date_from, date_to)

        base = apply_visit_filters(db.query(Visit), d_from, d_to, lane, service_id_int, q)

        total_records = base.count()                                   # count BEFORE offset/limit
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
        like = f"%{q}%"
        query = query.filter(
            Visit.ticket_number.ilike(like) | Visit.citizen_name.ilike(like)
        )
    return query


@app.get("/attendance/print", dependencies=[Depends(login_required)])
def attendance_print(request: Request, date_from: str = "", date_to: str = "", lane: str = "", service_id: str = "", q: str = ""):
    
    service_id_int = int(service_id) if service_id else None
    
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
            
        log_action("Database", "Attendance Printed", f"Opened the attendance print view ({', '.join(parts)})", request)
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
        
def csv_safe(value) -> str:
    """Stop Excel from running a cell that starts with = + - @ as a formula."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


@app.get("/attendance/export", dependencies=[Depends(login_required)])
def attendance_export(request: Request, date_from: str = "", date_to: str = "", lane: str = "", service_id: str = "", q: str = ""):
    """Download the attendance table (same filters as the page) as a CSV file."""
    service_id_int = int(service_id) if service_id else None
    d_from, d_to = resolve_date_range(date_from, date_to)

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
@app.get("/services", dependencies=[Depends(login_required)])
def list_services(request: Request):
    db = SessionLocal()
    services = db.query(Service).all()
    today_start = datetime.combine(date.today(), datetime.min.time())
                    
    total_today = db.query(Ticket).filter(Ticket.created_at >= today_start).count()
    response =  templates.TemplateResponse(request, "services.html", {
        
        "services": services,
        "total_today": total_today
    })
    db.close()
    return response

@app.post("/services/add", dependencies=[Depends(login_required)])
def add_services(request: Request, service_name: str = Form(...), service_status: StatusEnum = Form(alias="service_status")):
    db = SessionLocal()
    new_service = Service(service_name=service_name, service_status=service_status)
    db.add(new_service)
    db.commit()
    db.close()
    log_action("Service", "Service Added", f"Added service: {service_name} ({service_status.value})", request)
    return RedirectResponse(url="/services", status_code=303)

@app.post("/services/edit/{service_id}", dependencies=[Depends(login_required)])
def edit_service(request: Request, service_id: int, service_name: str = Form(...), service_status: StatusEnum = Form(alias="service_status")):
    db = SessionLocal()
    service = db.query(Service).filter(Service.id == service_id).first()
    changes = []
    
    if service:
        if service.service_name != service_name:
            changes.append(f'renamed from "{service.service_name}"')
        if service.service_status != service_status.value:
            changes.append(f"status {service.service_status} -> {service_status.value}")
        service.service_name = service_name
        service.service_status = service_status
        db.commit()
    db.close()
    if changes:   # saving the form without changing anything is not worth a log row
        log_action("Service", "Service Modified", f'Modified service "{service_name}": ' + "; ".join(changes), request)

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
                db.delete(service)
                log_entry = ("Service Deleted", f'Deleted service: {name}')
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
    page: int = 1,
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
            like = f"%{q}%"
            query = query.filter(
                AdminLog.admin_username.ilike(like)
                | AdminLog.action.ilike(like)
                | AdminLog.description.ilike(like)
            )

        total_records = query.count()
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

        target = target_dir / f"barangay_system_{datetime.now():%Y%m%d_%H%M%S}.db"
        source = sqlite3.connect(data_path("barangay_system.db"))
        try:
            dest = sqlite3.connect(str(target))
            try:
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()
    except Exception as e:
        flash(request, False, f"Backup failed: {e}")
        log_action("Database", "Backup Failed", f"Backup to {target_dir} failed: {e}", request)
    else:
        flash(request, True, f"Backup saved: {target}")
        log_action("Database", "Backup", f"Database backup saved to {target}", request)
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
            except Exception as e:
                print(f"VACUUM failed: {e}")
            log_action("Database", "Data Purge",
                       f"Purged {deleted} attendance record(s) from before {cutoff_date.strftime('%b %d, %Y')}", request)
            flash(request, True, f"Purged {deleted} attendance record(s) from before {cutoff_date.strftime('%b %d, %Y')}.")
        else:
            flash(request, True, f"No attendance records exist from before {cutoff_date.strftime('%b %d, %Y')}. Nothing was deleted.")
    return RedirectResponse(url="/maintenance", status_code=303)