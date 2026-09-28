from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi import Request 
from fastapi import Form
from fastapi.responses import RedirectResponse
from database import SessionLocal, Ticket, Service, Visit, Admin
from datetime import datetime, date
from starlette.middleware.sessions import SessionMiddleware
from enum import Enum
from fastapi import WebSocket, WebSocketDisconnect
from collections import Counter
from sqlalchemy import desc
import secrets
import string
import bcrypt


# For authentication(recovery key)
import secrets
import string

app = FastAPI()
templates = Jinja2Templates(directory="templates")
priority_streak = 0

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
# TODO: 
# 1. when opening the app, it will show first the setup 1 and 2. but the setup was done, it will direct to the login.
# 2. when i click forgot password, it will go to the forgot password page.
# 3. after the setup, it wil go to the login page.


@app.get("/")
def read_root():
    return RedirectResponse(url="/dashboard", status_code = 303)



@app.get("/dashboard")
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
def show_display(request: Request, announce: str = ""):
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
def show_dislay(request: Request):
    db = SessionLocal()
    today_start = datetime.combine(date.today(), datetime.min.time())
                
    total_today = db.query(Ticket).filter(Ticket.created_at >= today_start).count()
    response =  templates.TemplateResponse(request, "attendance.html", {
        
        "total_today": total_today
    })
    db.close()
    return response


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

