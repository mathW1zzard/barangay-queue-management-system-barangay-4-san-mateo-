from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi import Request 
from fastapi import Form
from fastapi.responses import RedirectResponse
from database import SessionLocal, Ticket, Service, Visit, Admin
from datetime import datetime, date, time
from starlette.middleware.sessions import SessionMiddleware
from enum import Enum
from fastapi import WebSocket, WebSocketDisconnect
from collections import Counter
from sqlalchemy import desc
import pyttsx3
import secrets
import string
import bcrypt
import asyncio
import math

# For authentication(recovery key)
import secrets
import string

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key="replace-this-with-a-random-secret-key")
templates = Jinja2Templates(directory="templates")
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
class StatusEnum(str, Enum):
    ACTIVE = "Active"
    INACTIVE = "Inactive"
    
app.mount("/static", StaticFiles(directory="static"), name="static")

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
def announce_ticket(ticket_number: str):
    engine = pyttsx3.init()
    engine.say(f"Ticket number {ticket_number}, please proceed to the counter")
    engine.runAndWait()



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
    if password != confirm_password:
        return templates.TemplateResponse(request, "setup1.html", {"error": "Passwords do not match"})

    if len(password) < 8:
        return templates.TemplateResponse(request, "setup1.html", {"error": "Password must be at least 8 characters"})

    recovery_key = generate_recovery_key()

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    recovery_key_hash = bcrypt.hashpw(recovery_key.encode(), bcrypt.gensalt()).decode()

    db = SessionLocal()
    new_admin = Admin(username=username, password_hash=password_hash, recovery_key_hash=recovery_key_hash)
    db.add(new_admin)
    db.commit()
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
    return RedirectResponse(url="/display", status_code=303)



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

def require_login(request: Request):
    if "admin_id" not in request.session:
        return RedirectResponse(url="/login", status_code=303)
    return None

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

@app.get("/dashboard")
def show_dashboard(request: Request, announce: str = ""):
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
        "service_volume": service_volume,
        "announce": announce,
    })
    db.close()
    return response

@app.get("/client_registration")
def show_registration(request: Request):
    db = SessionLocal()
    tickets = db.query(Ticket).all()
    services = db.query(Service).all()
    
    today_start = datetime.combine(date.today(), datetime.min.time())
    
    total_today = db.query(Ticket).filter(Ticket.created_at >= today_start).count()
    
    response =  templates.TemplateResponse(request, "client_registration.html", {
        "tickets": tickets,
        "services": services,
        "total_today": total_today
    })
    db.close()
    return response

@app.post("/register")
def register_ticket(citizen_name: str =  Form(...), lane: str = Form(...), service_id: int = Form(...)):
    db = SessionLocal()
    
    today_start = datetime.combine(date.today(), datetime.min.time()) # start at 00:00:00 for today
    
    active_today = db.query(Ticket).filter(Ticket.lane == lane, Ticket.created_at >= today_start).count()
    archive_today = db.query(Visit).filter(Visit.lane == lane, Visit.created_at >= today_start).count()
    
    
    new_ticket_number = f"{lane}-{active_today + archive_today + 1:03d}"

    new_ticket = Ticket(citizen_name=citizen_name, lane=lane, service_id=service_id, ticket_number=new_ticket_number, created_at=datetime.now())
    db.add(new_ticket)
    db.commit()
    db.close()
    
    return RedirectResponse(url="/client_registration", status_code=303)


# QUEUE MANAGEMENT
@app.get("/queue_management")
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
    response =  templates.TemplateResponse(request, "queue_management.html", {
        
        "total_today": total_today,
        "tickets": tickets,
        "services": services,
        "all_active": all_active,
        "waiting_count": waiting_count,
        "called_count": called_count,
        "skipped_count": skipped_count,
        "current_ticket": current_ticket,
        "announce": announce,
        "upcomming": upcomming
    })
    db.close()
    return response


# LIVE PANEL
@app.post("/call-next")
def call_next():
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
        announce = next_ticket.ticket_number
        db.commit()
    finally:
        db.close()

    return RedirectResponse(url=f"/queue_management?announce={announce}", status_code=303)

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
    

@app.post("/skip/{ticket_id}")
def skip_ticket(ticket_id: int):
    db = SessionLocal()
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if ticket:
        ticket.status = "skipped"   # just a status change, NOT archived
        db.commit()
    db.close()
    return RedirectResponse(url="/queue_management", status_code=303)


@app.post("/recall/{ticket_id}")
def recall_ticket(ticket_id: int):
    db = SessionLocal()
    
    if db.query(Ticket).filter(Ticket.status == "called").first():
        db.close()
        return RedirectResponse(url="/queue_management", status_code=303)
    
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    announce = ""
    if ticket:
        ticket.status = "called"
        ticket.called_at = datetime.now()  
        announce = ticket.ticket_number
        db.commit()
    db.close()
    return RedirectResponse(url=f"/queue_management?announce={announce}", status_code=303)

@app.post("/complete/{ticket_id}")
def complete_ticket(ticket_id: int):
    archive_ticket(ticket_id, "completed")
    db = SessionLocal()
    
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    
    if ticket:
        db.delete(ticket)
        db.commit()
    db.close()
    return RedirectResponse(url="/queue_management", status_code=303)


@app.post("/no-show/{ticket_id}")
def no_show_ticket(ticket_id: int):
    archive_ticket(ticket_id, "no_show")   # reuses your existing archive helper - permanent
    db = SessionLocal()
        
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        
    if ticket:
        db.delete(ticket)
        db.commit()
    db.close()
    return RedirectResponse(url="/queue_management", status_code=303)


@app.get("/attendance")
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


@app.get("/attendance/print")
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
@app.get("/services")
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

@app.post("/services/add")
def add_services(service_name: str = Form(...), service_status: StatusEnum = Form(alias="service_status")):
    db = SessionLocal()
    new_service = Service(service_name=service_name, service_status=service_status)
    db.add(new_service)
    db.commit()
    db.close()
    return RedirectResponse(url="/services", status_code=303)

@app.post("/services/edit/{service_id}")
def edit_service(service_id: int, service_name: str = Form(...), service_status: StatusEnum = Form(alias="service_status")):
    db = SessionLocal()
    service = db.query(Service).filter(Service.id == service_id).first()
    
    if service:
        service.service_name = service_name
        service.service_status = service_status
        db.commit()
    db.close()
    return RedirectResponse(url="/services", status_code=303)

@app.post("/services/delete/{service_id}")
def delete_service(service_id: int):
    db = SessionLocal()
    service = db.query(Service).filter(Service.id == service_id).first()
    
    if service:
        db.delete(service)
        db.commit()
    db.close()
    return RedirectResponse(url="/services", status_code=303)