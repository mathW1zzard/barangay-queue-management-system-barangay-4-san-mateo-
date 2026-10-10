from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime
from pathlib import Path

from paths import data_path

# Connect to sqlite called tickets.db (automatic create database)
# Development: the project folder. Packaged app: %LOCALAPPDATA%\BarangayQueue (see paths.py)
engine = create_engine(f"sqlite:///{Path(data_path('barangay_system.db')).as_posix()}")

# Base class that all our table models will inherit from
Base = declarative_base()

# Creates usable session
SessionLocal = sessionmaker(bind=engine)

# Services
class Service(Base):        
    __tablename__ = "services"
    
    id = Column(Integer, primary_key=True, index=True)
    service_name = Column(String)
    service_status = Column(String)

    tickets = relationship("Ticket", back_populates="service")
    
# Tickets(live panel showing the status of the ticket for that day only)
class Ticket(Base):
    __tablename__ = "tickets"
    
    id = Column(Integer, primary_key=True, index=True)
    citizen_name = Column(String)
    lane = Column(String)
    ticket_number = Column(String)
    status = Column(String, default="waiting")
    
    service_id = Column(Integer, ForeignKey("services.id"))
    
    created_at = Column(DateTime, default=datetime.now)
    called_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    
    service = relationship("Service", back_populates="tickets")
    
# History of all the records
class Visit(Base):
    __tablename__ = "visits"
    
    id = Column(Integer, primary_key=True, index=True)
    citizen_name = Column(String)
    lane = Column(String)
    ticket_number = Column(String)
    final_status = Column(String)
    
    service_id = Column(Integer, ForeignKey("services.id"))
    
    service = relationship("Service")
    
    created_at = Column(DateTime)
    called_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    
class Admin(Base):
    __tablename__ = "admins"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True)
    password_hash = Column(String)
    recovery_key_hash = Column(String, nullable=True)
    
class AdminLog(Base):
    __tablename__ = "admin_logs"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    # A copy of the name, not a foreign key, so the entry stays readable if the account changes
    admin_username = Column(String)
    category = Column(String, index=True)   # Queue / Service / Settings / Security / Database
    action = Column(String)                 # short label, e.g. "Ticket Skipped"
    description = Column(String)            # the readable sentence
    
Base.metadata.create_all(bind=engine)