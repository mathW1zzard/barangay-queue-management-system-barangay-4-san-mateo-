from fastapi import FastAPI, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi import Request 
from fastapi import Form
from fastapi import BackgroundTasks
from fastapi import Depends
from fastapi.responses import RedirectResponse
from database import SessionLocal, Ticket, Service, Visit, Admin
from paths import resource_path, data_path
from datetime import datetime, date, time
from starlette.middleware.sessions import SessionMiddleware
from enum import Enum
from fastapi import WebSocket, WebSocketDisconnect
from escpos.printer import Dummy, Win32Raw
from collections import Counter
from sqlalchemy import desc
from pathlib import Path
import secrets
import string
import bcrypt
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

        already_setup = db.query(Admin).filter()
        if already_setup is None:
            db.close()
            
        new_admin = Admin(username=username, password_hash=password_hash, recovery_key_hash=recovery_key_hash)
        db.add(new_admin)
        db.commit()
    finally:
        db.close()

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
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid username or password"})

    request.session["admin_id"] = admin.id
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
        return templates.TemplateResponse(request, "forgot_password.html", {"error": error})

    # Reset password AND issue a brand new recovery key (the old one was just used, so retire it)
    new_recovery_key = generate_recovery_key()
    admin.password_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    admin.recovery_key_hash = bcrypt.hashpw(new_recovery_key.encode(), bcrypt.gensalt()).decode()
    db.commit()
    db.close()

    return templates.TemplateResponse(request, "recovery_key_display.html", {"recovery_key": new_recovery_key})

@app.post("/logout")
def logout(request: Request):
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
            return templates.TemplateResponse(request, "change_password.html", {"error": error})

        admin.password_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
        db.commit()
    finally:
        db.close()

    return templates.TemplateResponse(request, "change_password.html", {"success": "Password updated."})

def generate_recovery_key():
    """Generates a key like BQS-7F3K-9D2M-XQ1R"""
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
    })
    db.close()
    return response

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
def print_ticket_again(ticket_id: int):
    db = SessionLocal()
    try:
        ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if ticket is None:
            return RedirectResponse(url="/client_registration", status_code=303)
        sent = print_ticket(ticket.ticket_number, ticket.citizen_name,
                            ticket.service.service_name, ticket.lane, ticket.created_at)
    finally:
        db.close()
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
def delete_ticket(ticket_id: int):
    """Remove a ticket from the live queue. Nothing is written to attendance history."""
    db = SessionLocal()
    try:
        ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if ticket:
            db.delete(ticket)
            db.commit()
    finally:
        db.close()
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
def call_next(background_tasks: BackgroundTasks):
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
def skip_ticket(ticket_id: int):
    db = SessionLocal()
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if ticket:
        ticket.status = "skipped"   # just a status change, NOT archived
        db.commit()
    db.close()
    return RedirectResponse(url="/queue_management", status_code=303)


@app.post("/recall/{ticket_id}", dependencies=[Depends(login_required)])
def recall_ticket(ticket_id: int, background_tasks: BackgroundTasks):
    db = SessionLocal()
    try:
        if db.query(Ticket).filter(Ticket.status == "called").first():
            return RedirectResponse(url="/queue_management", status_code=303)
        
        ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if ticket and ticket.status == "skipped":
            ticket.status = "called"
            ticket.called_at = datetime.now()  
            number = ticket.ticket_number
            db.commit()
            
            return RedirectResponse(url=f"/queue_management?announce={number}",status_code=303)
            
    finally:
        db.close()
        
    return RedirectResponse(url=f"/queue_management?announce={number}", status_code=303)

@app.post("/complete/{ticket_id}", dependencies=[Depends(login_required)])
def complete_ticket(ticket_id: int):
    archive_ticket(ticket_id, "completed")
    db = SessionLocal()
    
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    
    if ticket:
        db.delete(ticket)
        db.commit()
    db.close()
    return RedirectResponse(url="/queue_management", status_code=303)


@app.post("/no-show/{ticket_id}", dependencies=[Depends(login_required)])
def no_show_ticket(ticket_id: int):
    archive_ticket(ticket_id, "no_show")   # reuses your existing archive helper - permanent
    db = SessionLocal()
        
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        
    if ticket:
        db.delete(ticket)
        db.commit()
    db.close()
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
def add_services(service_name: str = Form(...), service_status: StatusEnum = Form(alias="service_status")):
    db = SessionLocal()
    new_service = Service(service_name=service_name, service_status=service_status)
    db.add(new_service)
    db.commit()
    db.close()
    return RedirectResponse(url="/services", status_code=303)

@app.post("/services/edit/{service_id}", dependencies=[Depends(login_required)])
def edit_service(service_id: int, service_name: str = Form(...), service_status: StatusEnum = Form(alias="service_status")):
    db = SessionLocal()
    service = db.query(Service).filter(Service.id == service_id).first()
    
    if service:
        service.service_name = service_name
        service.service_status = service_status
        db.commit()
    db.close()
    return RedirectResponse(url="/services", status_code=303)

@app.post("/services/delete/{service_id}", dependencies=[Depends(login_required)])
def delete_service(service_id: int):
    db = SessionLocal()
    try:
        service = db.query(Service).filter(Service.id == service_id).first()

        if service:
            has_history = (
                db.query(Ticket).filter(Ticket.service_id == service_id).first() is not None
                or db.query(Visit).filter(Visit.service_id == service_id).first() is not None
            )
            if has_history:
                # Old tickets and visits still point at this service, so keep the row
                service.service_status = "Inactive"
            else:
                db.delete(service)
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/services", status_code=303)
