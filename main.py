from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request
from fastapi import Form
from enum import Enum
from fastapi.responses import RedirectResponse
from database import SessionLocal, Service, Ticket, Visit, Admin
from datetime import datetime, date

# For authentication(recovery key)
import secrets
import string

app = FastAPI()
templates = Jinja2Templates(directory="templates")
priority_streak = 0

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
def show_dislay(request: Request):
    response =  templates.TemplateResponse(request, "dashboard.html", {
        
    })
    return response


@app.get("/client-registration")
def show_dislay(request: Request):
    response =  templates.TemplateResponse(request, "client_registration.html", {
        
    })
    return response

@app.get("/queue-management")
def show_dislay(request: Request):
    response =  templates.TemplateResponse(request, "queue_management.html", {
        
    })
    return response

@app.get("/attendance")
def show_dislay(request: Request):
    response =  templates.TemplateResponse(request, "attendance.html", {
        
    })
    return response


# SERVICE
@app.get("/services")
def list_services(request: Request):
    db = SessionLocal()
    services = db.query(Service).all()
    
    response =  templates.TemplateResponse(request, "services.html", {
        
        "services": services
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

