from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi import Request
from fastapi import Form
from fastapi.responses import RedirectResponse
from database import SessionLocal, Service, Ticket, Visit, Admin
from datetime import datetime, date

# For authentication(recovery key)
import secrets
import string

app = FastAPI()
templates = Jinja2Templates(directory="templates")
priority_streak = 0
# TODO: 
# 1. when opening the app, it will show first the setup 1 and 2. but the setup was done, it will direct to the login.
# 2. when i click forgot password, it will go to the forgot password page.
# 3. after the setup, it wil go to the login page.
# 4. after the login was verified, it will go to dashboard first
# 5. in all the pages, has 5 tabs on the left; wherein lciking one of them(Dashboard, Client Registration, Queue Management, Attendance, and Services)

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

@app.get("/services")
def show_dislay(request: Request):
    response =  templates.TemplateResponse(request, "services.html", {
        
    })
    return response