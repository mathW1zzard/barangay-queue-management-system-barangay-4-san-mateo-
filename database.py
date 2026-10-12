from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime
from pathlib import Path
import os, shutil, sqlite3
from sqlalchemy import event
from paths import data_path

# Connect to sqlite called tickets.db (automatic create database)
# Development: the project folder. Packaged app: %LOCALAPPDATA%\BarangayQueue (see paths.py)
engine = create_engine(f"sqlite:///{Path(data_path('barangay_system.db')).as_posix()}", connect_args={"timeout": 15})

@event.listens_for(engine, "connect")
def _sqlite_on_connect(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")              # SQLite ignores FOREIGN KEY unless asked
    cur.close()
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
    status = Column(String, default="waiting", index=True)
    
    service_id = Column(Integer, ForeignKey("services.id"))
    
    created_at = Column(DateTime, default=datetime.now, index=True)
    called_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    
    service = relationship("Service", back_populates="tickets")
    @property
    def service_label(self) -> str:
        return self.service.service_name if self.service else "Removed service"
    
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
    
    created_at = Column(DateTime, index=True)
    called_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    @property
    def service_label(self) -> str:
        return self.service.service_name if self.service else "Removed service"
    
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
    
class DailyCounter(Base):
    __tablename__ = "daily_counters"
    day = Column(String, primary_key=True)        # "2026-10-11"
    lane = Column(String, primary_key=True)       # "P" or "R"
    last_number = Column(Integer, nullable=False, default=0)
    
from app_logging import log

# --- Migrations: append new functions, never edit old ones ----------------
def _v1_counters_and_indexes(con):
    con.execute("""CREATE TABLE IF NOT EXISTS daily_counters (
        day VARCHAR NOT NULL, lane VARCHAR NOT NULL,
        last_number INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, lane))""")
    # Seed from existing data so numbers already issued are never reused
    con.execute("""INSERT OR IGNORE INTO daily_counters (day, lane, last_number)
        SELECT date(created_at), lane, MAX(CAST(substr(ticket_number, 3) AS INTEGER))
        FROM (SELECT created_at, lane, ticket_number FROM tickets
              UNION ALL SELECT created_at, lane, ticket_number FROM visits)
        WHERE created_at IS NOT NULL AND ticket_number LIKE '_-%'
        GROUP BY date(created_at), lane""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_tickets_created_at ON tickets (created_at)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets (status)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_visits_created_at ON visits (created_at)")
    try:   # backstop for #2: the database itself refuses a duplicate number on the same day
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_tickets_day_number "
                    "ON tickets (ticket_number, date(created_at))")
    except sqlite3.IntegrityError:
        log.warning("Duplicate ticket numbers already exist; unique index skipped")

MIGRATIONS = [_v1_counters_and_indexes]
LATEST = len(MIGRATIONS)

def _has_table(db_file, name):
    con = sqlite3.connect(db_file)
    try:
        return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                           (name,)).fetchone() is not None
    finally:
        con.close()

def _apply_migrations(db_file):
    con = sqlite3.connect(db_file, isolation_level=None, timeout=15)   # manual transactions
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        for target, step in enumerate(MIGRATIONS, start=1):
            if version >= target:
                continue
            con.execute("BEGIN IMMEDIATE")
            try:
                step(con)
                con.execute(f"PRAGMA user_version = {target}")   # committed with the step
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
    finally:
        con.close()

def run_migrations():
    db_file = data_path("barangay_system.db")
    existing = os.path.exists(db_file) and _has_table(db_file, "tickets")
    if existing:
        con = sqlite3.connect(db_file)
        behind = con.execute("PRAGMA user_version").fetchone()[0] < LATEST
        con.close()
        if behind:   # safety copy before touching a real installation
            shutil.copy2(db_file, f"{db_file}.before_v{LATEST}.bak")
        _apply_migrations(db_file)
    Base.metadata.create_all(bind=engine)          # brand-new tables for fresh installs
    if not existing:
        _apply_migrations(db_file)                 # stamps the version on a fresh DB

run_migrations()