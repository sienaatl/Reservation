import os
import uuid
import secrets
import smtplib
import ssl
import csv
import io
import json
from functools import wraps
from email.message import EmailMessage
from datetime import datetime, timedelta
from urllib.parse import quote
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, abort, session, Response
from werkzeug.security import generate_password_hash, check_password_hash
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
import psycopg2
from psycopg2.extras import RealDictCursor

APP_NAME = os.getenv("APP_NAME", "Siena Reservations")
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_PIN = os.getenv("ADMIN_PIN", "2468")
SLOT_MINUTES = int(os.getenv("SLOT_MINUTES", "30"))
TURN_MINUTES = int(os.getenv("TURN_MINUTES", "90"))
OPEN_HOUR = int(os.getenv("OPEN_HOUR", "16"))
CLOSE_HOUR = int(os.getenv("CLOSE_HOUR", "22"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", SMTP_USERNAME)
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Siena Restaurant and Bar")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes", "on"}
RESTAURANT_PHONE = os.getenv("RESTAURANT_PHONE", "404-488-3399")
RESTAURANT_ADDRESS = os.getenv("RESTAURANT_ADDRESS", "124 Devore Rd, Alpharetta, GA 30009")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
SMS_ENABLED = os.getenv("SMS_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
SMS_REMINDER_24H = os.getenv("SMS_REMINDER_24H", "true").lower() in {"1", "true", "yes", "on"}
SMS_REMINDER_2H = os.getenv("SMS_REMINDER_2H", "true").lower() in {"1", "true", "yes", "on"}
CRON_SECRET = os.getenv("CRON_SECRET", "")
REVIEW_URL = os.getenv("REVIEW_URL", "https://www.google.com/search?q=Siena+Restaurant+Alpharetta+reviews")
BIRTHDAY_SMS_ENABLED = os.getenv("BIRTHDAY_SMS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
REVIEW_SMS_ENABLED = os.getenv("REVIEW_SMS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
RUNNING_LATE_MINUTES = int(os.getenv("RUNNING_LATE_MINUTES", "15"))
RESTAURANT_TIMEZONE = os.getenv("RESTAURANT_TIMEZONE", "America/New_York")
SESSION_HOURS = int(os.getenv("SESSION_HOURS", "12"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-this-password")

# Fixed key for a Postgres advisory lock that serializes the booking
# check-then-insert path across concurrent requests (replaces SQLite's
# "BEGIN IMMEDIATE" write-lock behavior).
BOOKING_LOCK_KEY = 782342

# Fixed key for a Postgres advisory lock that serializes init_db() so
# multiple gunicorn workers booting at once don't race on schema creation.
INIT_DB_LOCK_KEY = 918273

ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me-in-production")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "true").lower() in {"1","true","yes","on"}, PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS))


@app.after_request
def add_cors_headers(response):
    # Only the public booking API is meant to be called from another origin.
    if ALLOWED_ORIGIN and request.path == "/api/book":
        response.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


class PGConnection:
    """Thin sqlite3-style wrapper around a psycopg2 connection so the rest of
    the app can keep using conn.execute(...)/executemany(...)/executescript(...)
    exactly like it did against sqlite3, including '?' placeholders."""

    def __init__(self, conn):
        self._conn = conn

    @staticmethod
    def _adapt(sql):
        return sql.replace("?", "%s")

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(self._adapt(sql), params)
        return cur

    def executemany(self, sql, seq_of_params):
        cur = self._conn.cursor()
        cur.executemany(self._adapt(sql), seq_of_params)
        return cur

    def executescript(self, sql):
        cur = self._conn.cursor()
        cur.execute(sql)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return PGConnection(conn)



def init_db():
    conn = db()
    # Session-level lock: held across every commit() below, released automatically
    # when conn.close() ends the session. Blocks other workers/replicas from
    # running schema setup concurrently instead of racing on it.
    conn.execute("SELECT pg_advisory_lock(?)", (INIT_DB_LOCK_KEY,))
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS restaurant_tables (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        capacity INTEGER NOT NULL,
        area TEXT NOT NULL DEFAULT 'Main Dining',
        x REAL NOT NULL DEFAULT 10,
        y REAL NOT NULL DEFAULT 10,
        width REAL NOT NULL DEFAULT 7,
        height REAL NOT NULL DEFAULT 12,
        shape TEXT NOT NULL DEFAULT 'rect',
        active INTEGER NOT NULL DEFAULT 1,
        server_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS reservations (
        id TEXT PRIMARY KEY,
        guest_name TEXT NOT NULL,
        email TEXT NOT NULL,
        phone TEXT NOT NULL,
        party_size INTEGER NOT NULL,
        reservation_at TEXT NOT NULL,
        duration_minutes INTEGER NOT NULL DEFAULT 90,
        table_id INTEGER,
        status TEXT NOT NULL DEFAULT 'confirmed',
        occasion TEXT,
        notes TEXT,
        manage_token TEXT,
        email_sent_at TEXT,
        sms_opt_in INTEGER NOT NULL DEFAULT 0,
        sms_confirmation_sent_at TEXT,
        sms_reminder_24h_sent_at TEXT,
        sms_reminder_2h_sent_at TEXT,
        sms_cancelled_sent_at TEXT,
        running_late_at TEXT,
        review_sms_sent_at TEXT,
        marketing_opt_in INTEGER NOT NULL DEFAULT 0,
        seated_at TEXT,
        completed_at TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(table_id) REFERENCES restaurant_tables(id)
    );

    CREATE TABLE IF NOT EXISTS table_combinations (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        area TEXT NOT NULL,
        capacity INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS table_combination_members (
        combination_id INTEGER NOT NULL REFERENCES table_combinations(id),
        table_id INTEGER NOT NULL REFERENCES restaurant_tables(id),
        PRIMARY KEY(combination_id, table_id)
    );

    CREATE TABLE IF NOT EXISTS reservation_tables (
        reservation_id TEXT NOT NULL REFERENCES reservations(id),
        table_id INTEGER NOT NULL REFERENCES restaurant_tables(id),
        PRIMARY KEY(reservation_id, table_id)
    );

    CREATE TABLE IF NOT EXISTS servers (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        active INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS waitlist (
        id TEXT PRIMARY KEY,
        guest_name TEXT NOT NULL,
        phone TEXT NOT NULL,
        party_size INTEGER NOT NULL,
        area_preference TEXT,
        quoted_minutes INTEGER NOT NULL DEFAULT 30,
        status TEXT NOT NULL DEFAULT 'waiting',
        notes TEXT,
        joined_at TEXT NOT NULL,
        notified_at TEXT,
        seated_at TEXT,
        reservation_id TEXT
    );

    CREATE TABLE IF NOT EXISTS guest_profiles (
        id SERIAL PRIMARY KEY,
        email TEXT,
        phone TEXT,
        guest_name TEXT NOT NULL,
        vip INTEGER NOT NULL DEFAULT 0,
        allergies TEXT,
        preferences TEXT,
        favorite_table TEXT,
        preferred_server TEXT,
        birthday TEXT,
        marketing_opt_in INTEGER NOT NULL DEFAULT 0,
        marketing_opt_out_at TEXT,
        birthday_sms_sent_year INTEGER,
        visit_count INTEGER NOT NULL DEFAULT 0,
        last_visit_at TEXT,
        UNIQUE(email, phone)
    );

    CREATE TABLE IF NOT EXISTS sms_campaigns (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        message TEXT NOT NULL,
        audience TEXT NOT NULL DEFAULT 'all',
        scheduled_at TEXT,
        sent_at TEXT,
        recipient_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS staff_users (
        id SERIAL PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'host',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        last_login_at TEXT
    );

    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS closures (
        id SERIAL PRIMARY KEY,
        closure_date TEXT NOT NULL,
        area TEXT NOT NULL DEFAULT 'All',
        reason TEXT,
        UNIQUE(closure_date, area)
    );

    CREATE TABLE IF NOT EXISTS audit_log (
        id SERIAL PRIMARY KEY,
        staff_username TEXT NOT NULL,
        action TEXT NOT NULL,
        entity_type TEXT,
        entity_id TEXT,
        details TEXT,
        created_at TEXT NOT NULL
    );
    """)
    conn.commit()

    # Additive columns for databases created before a given feature shipped.
    # Postgres' ADD COLUMN IF NOT EXISTS makes this safe to run unconditionally every boot.
    alter_statements = [
        "ALTER TABLE restaurant_tables ADD COLUMN IF NOT EXISTS x REAL NOT NULL DEFAULT 10",
        "ALTER TABLE restaurant_tables ADD COLUMN IF NOT EXISTS y REAL NOT NULL DEFAULT 10",
        "ALTER TABLE restaurant_tables ADD COLUMN IF NOT EXISTS width REAL NOT NULL DEFAULT 7",
        "ALTER TABLE restaurant_tables ADD COLUMN IF NOT EXISTS height REAL NOT NULL DEFAULT 12",
        "ALTER TABLE restaurant_tables ADD COLUMN IF NOT EXISTS shape TEXT NOT NULL DEFAULT 'rect'",
        "ALTER TABLE restaurant_tables ADD COLUMN IF NOT EXISTS active INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE restaurant_tables ADD COLUMN IF NOT EXISTS server_id INTEGER",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS manage_token TEXT",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS email_sent_at TEXT",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS sms_opt_in INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS sms_confirmation_sent_at TEXT",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS sms_reminder_24h_sent_at TEXT",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS sms_reminder_2h_sent_at TEXT",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS sms_cancelled_sent_at TEXT",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS running_late_at TEXT",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS review_sms_sent_at TEXT",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS marketing_opt_in INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS seated_at TEXT",
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS completed_at TEXT",
        "ALTER TABLE guest_profiles ADD COLUMN IF NOT EXISTS birthday TEXT",
        "ALTER TABLE guest_profiles ADD COLUMN IF NOT EXISTS marketing_opt_in INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE guest_profiles ADD COLUMN IF NOT EXISTS marketing_opt_out_at TEXT",
        "ALTER TABLE guest_profiles ADD COLUMN IF NOT EXISTS birthday_sms_sent_year INTEGER",
    ]
    for statement in alter_statements:
        conn.execute(statement)
    conn.commit()

    # Give existing reservations secure guest-management tokens.
    rows_without_tokens = conn.execute(
        "SELECT id FROM reservations WHERE manage_token IS NULL OR manage_token = ''"
    ).fetchall()
    for row in rows_without_tokens:
        conn.execute(
            "UPDATE reservations SET manage_token = ? WHERE id = ?",
            (secrets.token_urlsafe(32), row["id"])
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_reservations_manage_token ON reservations(manage_token)"
    )

    floor_tables = [
        # Main Dining Room
        ("P6",2,"Covered Patio",13,15,6,14,"rect"), ("P7",2,"Covered Patio",19,15,6,14,"rect"),
        ("P8",2,"Covered Patio",28,15,6,14,"rect"), ("P9",2,"Covered Patio",34,15,6,14,"rect"),
        ("P10",2,"Covered Patio",42,15,6,14,"rect"), ("P11",2,"Covered Patio",48,15,6,14,"rect"),
        ("P12",2,"Covered Patio",57,15,6,14,"rect"), ("P13",2,"Covered Patio",63,15,6,14,"rect"),
        ("P14",2,"Covered Patio",71,15,6,14,"rect"), ("P15",2,"Covered Patio",77,15,6,14,"rect"),
        ("2",2,"Covered Patio",91,14,6,12,"round"), ("4",4,"Covered Patio",1,43,6,12,"round"),
        ("P1",2,"Covered Patio",16,46,6,14,"rect"), ("P2",2,"Covered Patio",31,46,6,14,"rect"),
        ("P3",2,"Covered Patio",45,46,6,14,"rect"), ("P4",2,"Covered Patio",59,46,6,14,"rect"),
        ("P5",2,"Covered Patio",74,46,6,14,"rect"),
        ("P16",2,"Covered Patio",13,67,6,12,"rect"), ("P17",2,"Covered Patio",13,81,6,12,"rect"),
        ("P18",2,"Covered Patio",29,64,6,14,"rect"), ("P19",2,"Covered Patio",29,81,6,12,"rect"),
        ("P20",2,"Covered Patio",42,64,6,14,"rect"), ("P21",2,"Covered Patio",42,81,6,12,"rect"),
        ("P22",2,"Covered Patio",55,64,6,14,"rect"), ("P23",2,"Covered Patio",55,81,6,12,"rect"),
        ("P24",2,"Covered Patio",69,64,6,14,"rect"), ("P25",2,"Covered Patio",69,81,6,12,"rect"),
        ("3",3,"Covered Patio",92,73,6,12,"round"),        # Bar
        ("B1",1,"Bar",7,40,6,12,"round"),
        ("B2",1,"Bar",14,40,6,12,"round"),
        ("B3",1,"Bar",21,40,6,12,"round"),
        ("B4",1,"Bar",28,40,6,12,"round"),
        ("B5",1,"Bar",35,40,6,12,"round"),
        ("B6",1,"Bar",42,40,6,12,"round"),
        ("B7",1,"Bar",49,40,6,12,"round"),
        ("B8",1,"Bar",56,40,6,12,"round"),
        ("B9",1,"Bar",63,40,6,12,"round"),



        # Covered Patio
        ("D26",2,"Main Dining",65,7,6,12,"rect"), ("D27",2,"Main Dining",74,7,6,12,"rect"),
        ("D28",2,"Main Dining",82,7,6,12,"rect"), ("D29",2,"Main Dining",90,7,6,12,"rect"),
        ("D31",2,"Main Dining",16,62,6,14,"rect"), ("D30",2,"Main Dining",16,79,6,14,"rect"),
        ("D32",2,"Main Dining",28,62,6,14,"rect"), ("D33",2,"Main Dining",28,79,6,14,"rect"),
        ("D34",2,"Main Dining",40,62,6,14,"rect"), ("D35",2,"Main Dining",40,79,6,14,"rect"),
        ("D36",2,"Main Dining",51,62,6,14,"rect"), ("D37",2,"Main Dining",51,79,6,14,"rect"),

    ]

    conn.executemany("""
        INSERT INTO restaurant_tables(name, capacity, area, x, y, width, height, shape)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
          capacity=excluded.capacity, area=excluded.area, x=excluded.x, y=excluded.y,
          width=excluded.width, height=excluded.height, shape=excluded.shape, active=1
    """, floor_tables)

    combinations = {
        "P6+P7": ("Covered Patio",4,["P6","P7"]),
        "P8+P9": ("Covered Patio",4,["P8","P9"]),
        "P10+P11": ("Covered Patio",4,["P10","P11"]),
        "P12+P13": ("Covered Patio",4,["P12","P13"]),
        "P14+P15": ("Covered Patio",4,["P14","P15"]),
        "P16+P17": ("Covered Patio",4,["P16","P17"]),
        "P18+P19": ("Covered Patio",4,["P18","P19"]),
        "P20+P21": ("Covered Patio",4,["P20","P21"]),
        "P22+P23": ("Covered Patio",4,["P22","P23"]),
        "P24+P25": ("Covered Patio",4,["P24","P25"]),
        "D31+D30": ("Main Dining",4,["D31","D30"]),
        "D32+D33": ("Main Dining",4,["D32","D33"]),
        "D34+D35": ("Main Dining",4,["D34","D35"]),
        "D36+D37": ("Main Dining",4,["D36","D37"]),
    }
    for combo_name, (area, capacity, members) in combinations.items():
        conn.execute("""
            INSERT INTO table_combinations(name, area, capacity)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET area=excluded.area, capacity=excluded.capacity
        """, (combo_name, area, capacity))
        combo_id = conn.execute("SELECT id FROM table_combinations WHERE name=?", (combo_name,)).fetchone()["id"]
        conn.execute("DELETE FROM table_combination_members WHERE combination_id=?", (combo_id,))
        for member in members:
            table_id = conn.execute("SELECT id FROM restaurant_tables WHERE name=?", (member,)).fetchone()["id"]
            conn.execute(
                "INSERT INTO table_combination_members(combination_id, table_id) VALUES (?, ?) "
                "ON CONFLICT (combination_id, table_id) DO NOTHING",
                (combo_id, table_id)
            )

    conn.execute("""
        INSERT INTO reservation_tables(reservation_id, table_id)
        SELECT id, table_id FROM reservations WHERE table_id IS NOT NULL
        ON CONFLICT (reservation_id, table_id) DO NOTHING
    """)

    for server_name in ("Server 1", "Server 2", "Server 3", "Server 4"):
        conn.execute("INSERT INTO servers(name) VALUES (?) ON CONFLICT(name) DO NOTHING", (server_name,))

    defaults = {
        "open_hour": str(OPEN_HOUR), "close_hour": str(CLOSE_HOUR),
        "max_covers_per_slot": "30", "min_notice_minutes": "60",
        "booking_window_days": "90", "late_hold_minutes": "15",
        "same_day_cutoff_minutes": "30"
    }
    for key, value in defaults.items():
        conn.execute("INSERT INTO app_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO NOTHING", (key,value))
    if not conn.execute("SELECT 1 FROM staff_users LIMIT 1").fetchone():
        conn.execute("INSERT INTO staff_users(username,password_hash,role,created_at) VALUES (?,?,?,?)",
                     (ADMIN_USERNAME, generate_password_hash(ADMIN_PASSWORD), "manager", datetime.now().isoformat(timespec="seconds")))

    conn.commit()
    conn.close()


def public_url(endpoint: str, **values) -> str:
    relative = url_for(endpoint, **values)
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}{relative}"
    return f"{request.url_root.rstrip('/')}{relative}"


def format_reservation_datetime(value: str) -> str:
    return parse_dt(value).strftime("%A, %B %-d, %Y at %-I:%M %p")


def send_email(to_email: str, subject: str, html_body: str, text_body: str) -> bool:
    """Send through any SMTP provider. Booking still succeeds if email is unavailable."""
    if not SMTP_HOST or not SMTP_FROM_EMAIL or not to_email:
        app.logger.warning("Email not sent: SMTP_HOST/SMTP_FROM_EMAIL or recipient is missing.")
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
    message["To"] = to_email
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    try:
        if SMTP_PORT == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=20) as server:
                if SMTP_USERNAME:
                    server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(message)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
                server.ehlo()
                if SMTP_USE_TLS:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                if SMTP_USERNAME:
                    server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(message)
        return True
    except Exception:
        app.logger.exception("Unable to send reservation email.")
        return False


def send_reservation_email(reservation, kind: str = "confirmed") -> bool:
    manage_link = public_url("guest_manage", token=reservation["manage_token"])
    date_text = format_reservation_datetime(reservation["reservation_at"])
    status_labels = {
        "confirmed": ("Reservation confirmed", "Your table at Siena is confirmed."),
        "updated": ("Reservation updated", "Your Siena reservation has been updated."),
        "cancelled": ("Reservation cancelled", "Your Siena reservation has been cancelled."),
    }
    subject_prefix, heading = status_labels.get(kind, status_labels["confirmed"])
    subject = f"{subject_prefix} — {date_text}"

    details = f"""
Guest: {reservation['guest_name']}
Date and time: {date_text}
Party size: {reservation['party_size']} guests
Confirmation: {reservation['id']}
Status: {reservation['status'].replace('_', ' ').title()}
"""

    text_body = f"""{heading}

{details}
Manage or modify your reservation:
{manage_link}

Siena Restaurant and Bar
{RESTAURANT_ADDRESS}
{RESTAURANT_PHONE}
"""

    html_body = f"""<!doctype html>
<html>
<body style="margin:0;background:#f5f0e8;font-family:Arial,sans-serif;color:#211b18">
  <div style="max-width:620px;margin:32px auto;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid #ddd2c4">
    <div style="background:#080808;padding:26px;text-align:center">
      <div style="color:#fff;font-family:Georgia,serif;font-size:36px;font-style:italic">Siena</div>
      <div style="color:#c9a25d;letter-spacing:4px;font-size:10px">RESTAURANT AND BAR</div>
    </div>
    <div style="padding:30px">
      <p style="color:#8a641f;letter-spacing:2px;font-size:11px;font-weight:bold">{subject_prefix.upper()}</p>
      <h1 style="font-family:Georgia,serif;font-size:29px;margin:0 0 12px">{heading}</h1>
      <p style="color:#6f655e">Hello {reservation['guest_name']}, here are your reservation details.</p>
      <div style="background:#faf7f1;border:1px solid #e7ded2;border-radius:10px;padding:18px;line-height:1.9">
        <strong>Date and time:</strong> {date_text}<br>
        <strong>Party size:</strong> {reservation['party_size']} guests<br>
        <strong>Confirmation:</strong> {reservation['id']}<br>
        <strong>Status:</strong> {reservation['status'].replace('_', ' ').title()}
      </div>
      <a href="{manage_link}" style="display:block;text-align:center;margin:24px 0 10px;background:#6f1d2b;color:#fff;text-decoration:none;padding:15px;border-radius:8px;font-weight:bold">
        Manage or Modify Reservation
      </a>
      <p style="font-size:12px;color:#756b64;text-align:center">Use this private link to change or cancel your reservation.</p>
    </div>
    <div style="background:#111;color:#ddd;padding:20px;text-align:center;font-size:12px;line-height:1.7">
      {RESTAURANT_ADDRESS}<br>{RESTAURANT_PHONE}
    </div>
  </div>
</body>
</html>"""
    return send_email(reservation["email"], subject, html_body, text_body)


def normalize_us_phone(value: str) -> str:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if value and value.startswith("+") and 10 <= len(digits) <= 15:
        return "+" + digits
    return ""


def send_sms(to_phone: str, body: str) -> bool:
    """Send SMS through Twilio. Reservation actions still succeed if SMS is unavailable."""
    recipient = normalize_us_phone(to_phone)
    if not SMS_ENABLED or not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_PHONE_NUMBER:
        app.logger.warning("SMS not sent: Twilio is disabled or credentials are missing.")
        return False
    if not recipient:
        app.logger.warning("SMS not sent: invalid recipient phone number.")
        return False
    try:
        Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN).messages.create(
            body=body[:1600], from_=TWILIO_PHONE_NUMBER, to=recipient
        )
        return True
    except TwilioRestException:
        app.logger.exception("Unable to send Twilio SMS.")
        return False


def reservation_sms_text(reservation, kind: str = "confirmed") -> str:
    date_text = format_reservation_datetime(reservation["reservation_at"])
    manage_link = public_url("guest_manage", token=reservation["manage_token"])
    name = reservation["guest_name"].split()[0]
    if kind == "cancelled":
        return (f"Hi {name}, your Siena reservation for {date_text}, party of "
                f"{reservation['party_size']}, has been cancelled. {RESTAURANT_PHONE}")
    if kind == "updated":
        return (f"Hi {name}, your Siena reservation is updated: {date_text}, party of "
                f"{reservation['party_size']}. Manage: {manage_link}")
    if kind == "reminder_24h":
        return (f"Siena reminder: Hi {name}, your reservation is tomorrow at "
                f"{parse_dt(reservation['reservation_at']).strftime('%-I:%M %p')} for "
                f"{reservation['party_size']}. Manage: {manage_link}")
    if kind == "reminder_2h":
        late_link = public_url("running_late", token=reservation["manage_token"])
        return (f"Siena reminder: Hi {name}, we look forward to seeing you in about 2 hours at "
                f"{parse_dt(reservation['reservation_at']).strftime('%-I:%M %p')} for "
                f"{reservation['party_size']}. Running late? {late_link}")
    if kind == "running_late":
        return (f"Thanks {name}. We marked you as running about {RUNNING_LATE_MINUTES} minutes late. "
                f"Please call {RESTAURANT_PHONE} if your arrival changes.")
    if kind == "review":
        return (f"Thank you for dining at Siena, {name}! We would appreciate your feedback: {REVIEW_URL} "
                f"Reply STOP to opt out.")
    return (f"Hi {name}, your Siena reservation is confirmed for {date_text}, party of "
            f"{reservation['party_size']}. Manage or cancel: {manage_link}")


def send_reservation_sms(reservation, kind: str = "confirmed") -> bool:
    if not reservation["sms_opt_in"]:
        return False
    return send_sms(reservation["phone"], reservation_sms_text(reservation, kind))


def process_sms_reminders(now: datetime | None = None) -> dict:
    now = now or datetime.now()
    sent_24h = sent_2h = 0
    conn = db()
    rows = conn.execute("""
        SELECT * FROM reservations
        WHERE status='confirmed' AND sms_opt_in=1
          AND reservation_at::timestamp > ?::timestamp
          AND reservation_at::timestamp <= (?::timestamp + interval '25 hours')
        ORDER BY reservation_at::timestamp
    """, (now.isoformat(timespec="minutes"), now.isoformat(timespec="minutes"))).fetchall()
    for r in rows:
        at = parse_dt(r["reservation_at"])
        minutes = (at - now).total_seconds() / 60
        if SMS_REMINDER_24H and 1380 <= minutes <= 1500 and not r["sms_reminder_24h_sent_at"]:
            if send_reservation_sms(r, "reminder_24h"):
                conn.execute("UPDATE reservations SET sms_reminder_24h_sent_at=? WHERE id=?",
                             (now.isoformat(timespec="seconds"), r["id"]))
                sent_24h += 1
        if SMS_REMINDER_2H and 90 <= minutes <= 150 and not r["sms_reminder_2h_sent_at"]:
            if send_reservation_sms(r, "reminder_2h"):
                conn.execute("UPDATE reservations SET sms_reminder_2h_sent_at=? WHERE id=?",
                             (now.isoformat(timespec="seconds"), r["id"]))
                sent_2h += 1
    conn.commit(); conn.close()
    return {"sent_24h": sent_24h, "sent_2h": sent_2h}


@app.get("/tasks/send-sms-reminders")
def sms_reminder_task():
    if not CRON_SECRET or request.args.get("secret") != CRON_SECRET:
        abort(403)
    return jsonify({"ok": True, **process_sms_reminders()})


@app.get("/reservation/<token>/running-late")
def running_late(token):
    conn = db()
    reservation = conn.execute("SELECT * FROM reservations WHERE manage_token=?", (token,)).fetchone()
    if not reservation or reservation["status"] not in ("confirmed", "seated"):
        conn.close(); abort(404)
    now_value = datetime.now().isoformat(timespec="seconds")
    conn.execute("UPDATE reservations SET running_late_at=? WHERE id=?", (now_value, reservation["id"]))
    conn.commit()
    updated = conn.execute("SELECT * FROM reservations WHERE id=?", (reservation["id"],)).fetchone()
    conn.close()
    send_reservation_sms(updated, "running_late")
    return render_template("message_status.html", app_name=APP_NAME,
                           title="We received your update",
                           message=f"Your reservation has been marked approximately {RUNNING_LATE_MINUTES} minutes late. We will hold your table according to restaurant policy.")


def process_review_requests(now: datetime | None = None) -> dict:
    now = now or datetime.now()
    if not REVIEW_SMS_ENABLED:
        return {"review_sent": 0}
    conn = db(); sent = 0
    rows = conn.execute("""
        SELECT * FROM reservations
        WHERE status='completed' AND sms_opt_in=1
          AND review_sms_sent_at IS NULL
          AND completed_at IS NOT NULL
          AND completed_at::timestamp <= (?::timestamp - interval '60 minutes')
          AND completed_at::timestamp >= (?::timestamp - interval '3 days')
    """, (now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"))).fetchall()
    for r in rows:
        if send_reservation_sms(r, "review"):
            conn.execute("UPDATE reservations SET review_sms_sent_at=? WHERE id=?", (now.isoformat(timespec="seconds"), r["id"]))
            sent += 1
    conn.commit(); conn.close()
    return {"review_sent": sent}


def process_birthday_greetings(now: datetime | None = None) -> dict:
    now = now or datetime.now()
    if not BIRTHDAY_SMS_ENABLED:
        return {"birthday_sent": 0}
    conn = db(); sent = 0
    rows = conn.execute("""
        SELECT * FROM guest_profiles
        WHERE birthday IS NOT NULL AND birthday != ''
          AND marketing_opt_in=1 AND marketing_opt_out_at IS NULL
          AND to_char(birthday::date, 'MM-DD')=?
          AND COALESCE(birthday_sms_sent_year,0) != ?
    """, (now.strftime('%m-%d'), now.year)).fetchall()
    for g in rows:
        first = g["guest_name"].split()[0]
        text = (f"Happy Birthday {first}! Siena Restaurant wishes you a wonderful day. "
                f"Celebrate with us: {PUBLIC_BASE_URL or RESTAURANT_PHONE} Reply STOP to opt out.")
        if send_sms(g["phone"], text):
            conn.execute("UPDATE guest_profiles SET birthday_sms_sent_year=? WHERE id=?", (now.year, g["id"]))
            sent += 1
    conn.commit(); conn.close()
    return {"birthday_sent": sent}


@app.get("/tasks/send-guest-messages")
def guest_message_task():
    if not CRON_SECRET or request.args.get("secret") != CRON_SECRET:
        abort(403)
    return jsonify({"ok": True, **process_review_requests(), **process_birthday_greetings()})


@app.post("/twilio/incoming")
def twilio_incoming():
    phone = normalize_us_phone(request.form.get("From", ""))
    body = request.form.get("Body", "").strip().upper()
    if body in {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"} and phone:
        conn = db()
        conn.execute("UPDATE guest_profiles SET marketing_opt_in=0, marketing_opt_out_at=? WHERE phone=?",
                     (datetime.now().isoformat(timespec="seconds"), phone))
        conn.execute("UPDATE reservations SET marketing_opt_in=0 WHERE phone=?", (phone,))
        conn.commit(); conn.close()
    return ("", 204)


def reservation_by_token(token: str):
    conn = db()
    reservation = conn.execute(
        "SELECT * FROM reservations WHERE manage_token = ?",
        (token,)
    ).fetchone()
    conn.close()
    return reservation


def reservation_conflicts_on_table(conn, table_id: int, at: datetime, duration_minutes: int,
                                   exclude_reservation_id: str | None = None) -> bool:
    end = at + timedelta(minutes=duration_minutes)
    params = [table_id, end.isoformat(timespec="minutes"), at.isoformat(timespec="minutes")]
    exclude_sql = ""
    if exclude_reservation_id:
        exclude_sql = "AND r.id != ?"
        params.append(exclude_reservation_id)
    conflict = conn.execute(f"""
        SELECT 1
        FROM reservations r
        JOIN reservation_tables rt ON rt.reservation_id = r.id
        WHERE rt.table_id = ?
          AND r.status IN ('confirmed', 'seated', 'walk_in')
          AND (CASE WHEN r.status='seated' AND r.seated_at IS NOT NULL
                    THEN r.seated_at ELSE r.reservation_at END)::timestamp < ?::timestamp
          AND (CASE WHEN r.status='seated' AND r.seated_at IS NOT NULL
                    THEN r.seated_at ELSE r.reservation_at END)::timestamp
                + make_interval(mins => r.duration_minutes) > ?::timestamp
          {exclude_sql}
        LIMIT 1
    """, params).fetchone()
    return conflict is not None


def table_is_available_for_edit(table_id: int, at: datetime, duration_minutes: int, reservation_id: str) -> bool:
    conn = db()
    try:
        return not reservation_conflicts_on_table(conn, table_id, at, duration_minutes, reservation_id)
    finally:
        conn.close()


def get_setting(key: str, default=None):
    conn = db()
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def audit(action: str, entity_type: str = "", entity_id: str = "", details=None):
    username = session.get("staff_username", "system")
    conn = db()
    conn.execute("INSERT INTO audit_log(staff_username,action,entity_type,entity_id,details,created_at) VALUES (?,?,?,?,?,?)",
                 (username, action, entity_type, str(entity_id or ""), json.dumps(details or {}), datetime.now().isoformat(timespec="seconds")))
    conn.commit(); conn.close()


def current_staff():
    if not session.get("staff_user_id"):
        return None
    return {"id": session.get("staff_user_id"), "username": session.get("staff_username"), "role": session.get("staff_role")}


def role_required(*roles):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            staff = current_staff()
            if not staff:
                return redirect(url_for("staff_login", next=request.url))
            if roles and staff["role"] not in roles:
                abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return decorator


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def recommended_duration(area: str, party_size: int, at: datetime | None = None) -> int:
    """Siena turn-time profile. Managers can override per reservation."""
    if area == "Bar":
        return 60
    if area == "Covered Patio":
        return 90 if party_size <= 2 else 105
    if area == "Main Dining":
        if party_size <= 2:
            return 90
        if party_size <= 4:
            return 120
        if party_size <= 6:
            return 150
        return 180
    return TURN_MINUTES


def find_table_allocation(at: datetime, party_size: int, exclude_reservation_id: str | None = None):
    """Return the best same-area set of free tables whose seats cover the party."""
    conn = db()
    try:
        areas = ["Main Dining", "Covered Patio", "Bar"]
        candidates = []
        for area in areas:
            duration = recommended_duration(area, party_size, at)
            tables = conn.execute("""
                SELECT * FROM restaurant_tables
                WHERE area=? AND active=1
                ORDER BY capacity DESC, id
            """, (area,)).fetchall()
            free = [dict(t) for t in tables if not reservation_conflicts_on_table(
                conn, t["id"], at, duration, exclude_reservation_id
            )]
            # Dynamic programming: minimize excess seats, then number of tables.
            states = {0: []}
            for table in free:
                for total, chosen in list(states.items())[::-1]:
                    new_total = total + int(table["capacity"])
                    new_chosen = chosen + [table]
                    old = states.get(new_total)
                    if old is None or len(new_chosen) < len(old):
                        states[new_total] = new_chosen
            valid = [(total, chosen) for total, chosen in states.items() if total >= party_size]
            if valid:
                total, chosen = min(valid, key=lambda x: (x[0]-party_size, len(x[1]), x[0]))
                candidates.append({
                    "area": area,
                    "capacity": total,
                    "tables": chosen,
                    "table_ids": [t["id"] for t in chosen],
                    "table_names": [t["name"] for t in chosen],
                    "recommended_duration": duration,
                })
        if not candidates:
            return None
        return min(candidates, key=lambda x: (x["capacity"]-party_size, len(x["tables"]), areas.index(x["area"])))
    finally:
        conn.close()


def available_tables(at: datetime, party_size: int):
    allocation = find_table_allocation(at, party_size)
    return [allocation] if allocation else []

def generate_slots(date_str: str, party_size: int):
    date = datetime.strptime(date_str, "%Y-%m-%d")
    now = datetime.now()
    booking_window_days = int(get_setting("booking_window_days", "90"))
    min_notice = int(get_setting("min_notice_minutes", "60"))
    if date.date() > (now + timedelta(days=booking_window_days)).date():
        return []
    conn = db()
    closed = conn.execute("SELECT 1 FROM closures WHERE closure_date=? AND area='All'", (date_str,)).fetchone()
    conn.close()
    if closed:
        return []
    start = date.replace(hour=int(get_setting("open_hour", str(OPEN_HOUR))), minute=0)
    close = date.replace(hour=int(get_setting("close_hour", str(CLOSE_HOUR))), minute=0)
    max_covers = int(get_setting("max_covers_per_slot", "30"))
    slots = []
    current = start
    while current <= close:
        if current >= now + timedelta(minutes=min_notice):
            conn = db()
            slot_end = current + timedelta(minutes=SLOT_MINUTES)
            booked = conn.execute("SELECT COALESCE(SUM(party_size),0) n FROM reservations WHERE status NOT IN ('cancelled','no_show') AND reservation_at::timestamp>=?::timestamp AND reservation_at::timestamp<?::timestamp", (current.isoformat(timespec='minutes'), slot_end.isoformat(timespec='minutes'))).fetchone()["n"]
            conn.close()
            if booked + party_size <= max_covers and available_tables(current, party_size):
                slots.append(current)
        current += timedelta(minutes=SLOT_MINUTES)
    return slots


def require_admin():
    if current_staff():
        return
    pin = request.headers.get("X-Admin-Pin") or request.args.get("pin") or request.form.get("pin")
    if pin == ADMIN_PIN:
        session.permanent = True
        session["staff_user_id"] = 0
        session["staff_username"] = "legacy-pin"
        session["staff_role"] = "manager"
        return
    abort(401)


@app.errorhandler(401)
def handle_unauthorized(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Authentication required."}), 401
    return redirect(url_for("staff_login", next=request.url))


@app.route("/staff/login", methods=["GET", "POST"])
def staff_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = db(); user = conn.execute("SELECT * FROM staff_users WHERE username=? AND active=1", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear(); session.permanent = True
            session.update(staff_user_id=user["id"], staff_username=user["username"], staff_role=user["role"])
            conn.execute("UPDATE staff_users SET last_login_at=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"), user["id"]))
            conn.commit(); conn.close(); audit("login", "staff_user", user["id"]); return redirect(request.args.get("next") or url_for("manager_overview"))
        conn.close(); flash("Invalid username or password.", "error")
    return render_template("login.html", app_name=APP_NAME)


@app.post("/staff/logout")
def staff_logout():
    username=session.get("staff_username"); session.clear(); flash("You have been signed out.", "success"); return redirect(url_for("staff_login"))


@app.route("/settings", methods=["GET", "POST"])
@role_required("manager")
def settings_page():
    conn=db()
    if request.method=="POST":
        action=request.form.get("action","settings")
        if action=="settings":
            for key in ("open_hour","close_hour","max_covers_per_slot","min_notice_minutes","booking_window_days","late_hold_minutes","same_day_cutoff_minutes"):
                value=request.form.get(key,"").strip()
                if value: conn.execute("INSERT INTO app_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,value))
            conn.commit(); audit("update_settings", "settings")
        elif action=="closure":
            conn.execute("""
                INSERT INTO closures(closure_date,area,reason) VALUES (?,?,?)
                ON CONFLICT(closure_date,area) DO UPDATE SET reason=excluded.reason
            """,(request.form.get("closure_date"),request.form.get("area","All"),request.form.get("reason","")))
            conn.commit(); audit("add_closure", "closure", request.form.get("closure_date"))
        elif action=="delete_closure":
            conn.execute("DELETE FROM closures WHERE id=?",(request.form.get("closure_id"),)); conn.commit(); audit("delete_closure","closure",request.form.get("closure_id"))
        conn.close(); return redirect(url_for("settings_page"))
    settings={r["key"]:r["value"] for r in conn.execute("SELECT * FROM app_settings").fetchall()}
    closures=conn.execute("SELECT * FROM closures ORDER BY closure_date DESC").fetchall()
    users=conn.execute("SELECT id,username,role,active,last_login_at FROM staff_users ORDER BY username").fetchall(); conn.close()
    return render_template("settings.html", settings=settings, closures=closures, users=users, app_name=APP_NAME)


@app.post("/settings/staff")
@role_required("manager")
def add_staff_user():
    username=request.form.get("username","").strip(); password=request.form.get("password",""); role=request.form.get("role","host")
    if not username or len(password)<10 or role not in {"manager","host","server"}: flash("Use a username and a password of at least 10 characters.","error"); return redirect(url_for("settings_page"))
    conn=db()
    try: conn.execute("INSERT INTO staff_users(username,password_hash,role,created_at) VALUES (?,?,?,?)",(username,generate_password_hash(password),role,datetime.now().isoformat(timespec="seconds"))); conn.commit(); audit("create_staff","staff_user",username,{"role":role})
    except psycopg2.IntegrityError: conn.rollback(); flash("That username already exists.","error")
    finally: conn.close()
    return redirect(url_for("settings_page"))


@app.get("/admin/export.csv")
@role_required("manager","host")
def export_reservations():
    date_from=request.args.get("from","2000-01-01"); date_to=request.args.get("to","2100-01-01")
    conn=db(); rows=conn.execute("SELECT id,guest_name,email,phone,party_size,reservation_at,status,occasion,notes,created_at FROM reservations WHERE reservation_at::date BETWEEN ?::date AND ?::date ORDER BY reservation_at",(date_from,date_to)).fetchall(); conn.close()
    out=io.StringIO(); writer=csv.writer(out); writer.writerow(rows[0].keys() if rows else ["id","guest_name","email","phone","party_size","reservation_at","status","occasion","notes","created_at"]); writer.writerows([list(r.values()) for r in rows]); audit("export_reservations","reservation","",{"from":date_from,"to":date_to,"count":len(rows)})
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition":"attachment; filename=siena-reservations.csv"})


@app.get("/admin/audit")
@role_required("manager")
def audit_page():
    conn=db(); rows=conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 500").fetchall(); conn.close(); return render_template("audit.html", rows=rows, app_name=APP_NAME)


@app.get("/")
def index():
    return render_template("index.html", app_name=APP_NAME)


@app.get("/api/availability")
def availability():
    date_str = request.args.get("date", "")
    party_size = request.args.get("party_size", type=int)
    if not date_str or not party_size or party_size < 1 or party_size > 20:
        return jsonify({"error": "Valid date and party_size are required."}), 400
    try:
        slots = generate_slots(date_str, party_size)
    except ValueError:
        return jsonify({"error": "Date must use YYYY-MM-DD."}), 400
    return jsonify({
        "slots": [
            {"value": s.isoformat(timespec="minutes"), "label": s.strftime("%-I:%M %p")}
            for s in slots
        ]
    })


def _create_reservation(guest_name, email, phone, party_size, reservation_at_raw,
                         occasion, notes, sms_opt_in, marketing_opt_in):
    """Shared booking logic used by both the HTML form (/book) and the JSON
    API (/api/book). Returns a dict describing success or failure."""
    guest_name = (guest_name or "").strip()
    email = (email or "").strip()
    phone = (phone or "").strip()
    occasion = (occasion or "").strip()
    notes = (notes or "").strip()

    if not all([guest_name, email, phone, party_size, reservation_at_raw]):
        return {"ok": False, "status": 400, "error": "Please complete all required fields."}
    if not party_size or party_size < 1 or party_size > 20:
        return {"ok": False, "status": 400, "error": "Online reservations are available for parties of 1 to 20 guests."}

    try:
        reservation_at = parse_dt(reservation_at_raw)
    except ValueError:
        return {"ok": False, "status": 400, "error": "Please choose a valid reservation time."}

    conn = db()
    try:
        conn.execute("SELECT pg_advisory_xact_lock(?)", (BOOKING_LOCK_KEY,))
        tables = available_tables(reservation_at, party_size)
        if not tables:
            conn.rollback(); conn.close()
            return {"ok": False, "status": 409, "error": "That time is no longer available. Please choose another time."}
        chosen = tables[0]
        reservation_id = uuid.uuid4().hex[:10].upper()
        manage_token = secrets.token_urlsafe(32)
        conn.execute("""
            INSERT INTO reservations (
                id, guest_name, email, phone, party_size, reservation_at,
                duration_minutes, table_id, status, occasion, notes,
                manage_token, sms_opt_in, marketing_opt_in, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, ?, ?, ?)
        """, (
            reservation_id, guest_name, email, phone, party_size,
            reservation_at.isoformat(timespec="minutes"), chosen["recommended_duration"],
            chosen["table_ids"][0], occasion, notes, manage_token, sms_opt_in, marketing_opt_in,
            datetime.now().isoformat(timespec="seconds")
        ))
        conn.executemany(
            "INSERT INTO reservation_tables(reservation_id, table_id) VALUES (?, ?)",
            [(reservation_id, table_id) for table_id in chosen["table_ids"]]
        )
        conn.commit()
    except (psycopg2.OperationalError, psycopg2.IntegrityError):
        conn.rollback(); conn.close()
        return {"ok": False, "status": 409, "error": "Another booking was completed at the same time. Please select another slot."}

    reservation = conn.execute(
        "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
    ).fetchone()
    # Upsert matched on the exact (email, phone) pair the UNIQUE constraint enforces.
    # A prior version matched on email OR phone, which could find a different,
    # only-partially-matching row and then collide when trying to overwrite it.
    conn.execute("""
        INSERT INTO guest_profiles(email, phone, guest_name, marketing_opt_in)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (email, phone) DO UPDATE SET
            guest_name = excluded.guest_name,
            marketing_opt_in = excluded.marketing_opt_in,
            marketing_opt_out_at = CASE WHEN excluded.marketing_opt_in = 1 THEN NULL ELSE guest_profiles.marketing_opt_out_at END
    """, (email.lower(), phone, guest_name, marketing_opt_in))
    conn.commit()
    conn.close()

    email_sent = send_reservation_email(reservation, "confirmed")
    if email_sent:
        conn = db()
        conn.execute(
            "UPDATE reservations SET email_sent_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), reservation_id)
        )
        conn.commit()
        conn.close()

    sms_sent = False
    if sms_opt_in:
        sms_sent = send_reservation_sms(reservation, "confirmed")
        if sms_sent:
            conn = db()
            conn.execute("UPDATE reservations SET sms_confirmation_sent_at=? WHERE id=?",
                         (datetime.now().isoformat(timespec="seconds"), reservation_id))
            conn.commit(); conn.close()

    conn = db()
    reservation = conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
    conn.close()

    return {"ok": True, "status": 201, "reservation": reservation, "email_sent": email_sent, "sms_sent": sms_sent}


@app.post("/book")
def book():
    guest_name = request.form.get("guest_name", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    party_size = request.form.get("party_size", type=int)
    reservation_at_raw = request.form.get("reservation_at", "").strip()
    occasion = request.form.get("occasion", "").strip()
    notes = request.form.get("notes", "").strip()
    sms_opt_in = 1 if request.form.get("sms_opt_in") else 0
    marketing_opt_in = 1 if request.form.get("marketing_opt_in") else 0

    result = _create_reservation(guest_name, email, phone, party_size, reservation_at_raw,
                                  occasion, notes, sms_opt_in, marketing_opt_in)
    if not result["ok"]:
        flash(result["error"], "error")
        return redirect(url_for("index"))

    if not result["email_sent"]:
        flash("Reservation confirmed. Email delivery is not configured or could not be completed.", "error")
    if sms_opt_in and not result["sms_sent"] and SMS_ENABLED:
        flash("Reservation confirmed, but the confirmation text could not be delivered.", "error")

    return redirect(url_for("confirmation", reservation_id=result["reservation"]["id"]))


@app.route("/api/book", methods=["POST", "OPTIONS"])
def api_book():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(silent=True) or request.form
    guest_name = data.get("guest_name")
    email = data.get("email")
    phone = data.get("phone")
    try:
        party_size = int(data.get("party_size"))
    except (TypeError, ValueError):
        party_size = None
    reservation_at_raw = data.get("reservation_at")
    occasion = data.get("occasion")
    notes = data.get("notes")
    sms_opt_in = 1 if str(data.get("sms_opt_in") or "").lower() in {"1", "true", "on", "yes"} else 0
    marketing_opt_in = 1 if str(data.get("marketing_opt_in") or "").lower() in {"1", "true", "on", "yes"} else 0

    result = _create_reservation(guest_name, email, phone, party_size, reservation_at_raw,
                                  occasion, notes, sms_opt_in, marketing_opt_in)
    if not result["ok"]:
        return jsonify({"ok": False, "error": result["error"]}), result["status"]

    r = result["reservation"]
    return jsonify({
        "ok": True,
        "reservation": {
            "id": r["id"],
            "guest_name": r["guest_name"],
            "email": r["email"],
            "phone": r["phone"],
            "party_size": r["party_size"],
            "reservation_at": r["reservation_at"],
            "status": r["status"],
        },
        "manage_url": public_url("guest_manage", token=r["manage_token"]),
        "email_sent": result["email_sent"],
        "sms_sent": result["sms_sent"],
    }), result["status"]


@app.get("/confirmation/<reservation_id>")
def confirmation(reservation_id):
    conn = db()
    row = conn.execute("""
        SELECT r.*, t.name AS table_name, t.area
        FROM reservations r
        LEFT JOIN restaurant_tables t ON t.id = r.table_id
        WHERE r.id = ?
    """, (reservation_id,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    return render_template(
        "confirmation.html",
        reservation=row,
        manage_url=url_for("guest_manage", token=row["manage_token"]),
        app_name=APP_NAME
    )


@app.route("/manage/<token>", methods=["GET", "POST"])
def guest_manage(token):
    reservation = reservation_by_token(token)
    if not reservation:
        abort(404)

    if request.method == "POST":
        action = request.form.get("action", "update")
        conn = db()

        if action == "cancel":
            conn.execute(
                "UPDATE reservations SET status = 'cancelled' WHERE manage_token = ?",
                (token,)
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM reservations WHERE manage_token = ?", (token,)
            ).fetchone()
            conn.close()
            send_reservation_email(updated, "cancelled")
            if send_reservation_sms(updated, "cancelled"):
                conn2 = db(); conn2.execute("UPDATE reservations SET sms_cancelled_sent_at=? WHERE id=?",
                    (datetime.now().isoformat(timespec="seconds"), updated["id"])); conn2.commit(); conn2.close()
            flash("Your reservation has been cancelled.", "success")
            return redirect(url_for("guest_manage", token=token))

        guest_name = request.form.get("guest_name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        party_size = request.form.get("party_size", type=int)
        reservation_at_raw = request.form.get("reservation_at", "").strip()
        occasion = request.form.get("occasion", "").strip()
        notes = request.form.get("notes", "").strip()
        sms_opt_in = 1 if request.form.get("sms_opt_in") else 0

        if not all([guest_name, email, phone, party_size, reservation_at_raw]):
            conn.close()
            flash("Please complete all required fields.", "error")
            return redirect(url_for("guest_manage", token=token))

        try:
            new_at = parse_dt(reservation_at_raw)
        except ValueError:
            conn.close()
            flash("Please choose a valid reservation time.", "error")
            return redirect(url_for("guest_manage", token=token))

        if new_at <= datetime.now():
            conn.close()
            flash("Reservation time must be in the future.", "error")
            return redirect(url_for("guest_manage", token=token))

        # Keep the existing table when it fits and is conflict-free; otherwise assign a new table.
        table_id = reservation["table_id"]
        existing_table = None
        if table_id:
            existing_table = conn.execute(
                "SELECT * FROM restaurant_tables WHERE id = ?", (table_id,)
            ).fetchone()

        if (
            not existing_table
            or existing_table["capacity"] < party_size
            or not table_is_available_for_edit(
                table_id, new_at, reservation["duration_minutes"], reservation["id"]
            )
        ):
            end = new_at + timedelta(minutes=reservation["duration_minutes"])
            replacement = conn.execute("""
                SELECT t.*
                FROM restaurant_tables t
                WHERE t.capacity >= ?
                  AND t.active = 1
                  AND NOT EXISTS (
                    SELECT 1 FROM reservations r
                    WHERE r.table_id = t.id
                      AND r.id != ?
                      AND r.status IN ('confirmed','seated','walk_in')
                      AND r.reservation_at::timestamp < ?::timestamp
                      AND r.reservation_at::timestamp + make_interval(mins => r.duration_minutes) > ?::timestamp
                  )
                ORDER BY t.capacity, t.id
                LIMIT 1
            """, (
                party_size, reservation["id"],
                end.isoformat(timespec="minutes"), new_at.isoformat(timespec="minutes")
            )).fetchone()
            table_id = replacement["id"] if replacement else None

        if table_id is None:
            conn.close()
            flash("That date and time is unavailable for your party size. Please choose another time.", "error")
            return redirect(url_for("guest_manage", token=token))

        conn.execute("""
            UPDATE reservations
            SET guest_name = ?, email = ?, phone = ?, party_size = ?,
                reservation_at = ?, table_id = ?, status = 'confirmed',
                occasion = ?, notes = ?, sms_opt_in = ?,
                sms_reminder_24h_sent_at = NULL, sms_reminder_2h_sent_at = NULL
            WHERE manage_token = ?
        """, (
            guest_name, email, phone, party_size,
            new_at.isoformat(timespec="minutes"), table_id,
            occasion, notes, sms_opt_in, token
        ))
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM reservations WHERE manage_token = ?", (token,)
        ).fetchone()
        conn.close()

        send_reservation_email(updated, "updated")
        send_reservation_sms(updated, "updated")
        flash("Your reservation has been updated. Confirmation messages were sent where enabled.", "success")
        return redirect(url_for("guest_manage", token=token))

    return render_template(
        "manage.html",
        reservation=reservation,
        app_name=APP_NAME
    )


@app.post("/admin/resend-email/<reservation_id>")
def admin_resend_email(reservation_id):
    require_admin()
    conn = db()
    reservation = conn.execute(
        "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
    ).fetchone()
    conn.close()
    if not reservation:
        abort(404)

    kind = "cancelled" if reservation["status"] == "cancelled" else "updated"
    if send_reservation_email(reservation, kind):
        flash(f"Email sent to {reservation['email']}.", "success")
    else:
        flash("Email could not be sent. Check the SMTP environment settings.", "error")
    return redirect(url_for(
        "admin",
        date=reservation["reservation_at"][:10],
        pin=request.form.get("pin")
    ))


@app.post("/admin/resend-sms/<reservation_id>")
def admin_resend_sms(reservation_id):
    require_admin()
    conn = db(); reservation = conn.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone(); conn.close()
    if not reservation: abort(404)
    kind = "cancelled" if reservation["status"] == "cancelled" else "updated"
    if send_reservation_sms(reservation, kind):
        flash(f"SMS sent to {reservation['phone']}.", "success")
    else:
        flash("SMS could not be sent. Check Twilio settings and guest SMS consent.", "error")
    return redirect(url_for("admin", date=reservation["reservation_at"][:10], pin=request.form.get("pin")))


@app.get("/api/floorplan")
def floorplan_data():
    require_admin()
    area = request.args.get("area", "Main Dining")
    at_raw = request.args.get("at") or datetime.now().isoformat(timespec="minutes")
    try:
        at = parse_dt(at_raw)
    except ValueError:
        return jsonify({"error": "Invalid date/time."}), 400

    conn = db()
    tables = conn.execute("""
        SELECT * FROM restaurant_tables
        WHERE area = ? AND active = 1 AND area != 'Large Party'
        ORDER BY name
    """, (area,)).fetchall()

    result = []
    for table in tables:
        reservation = conn.execute("""
            SELECT id, guest_name, party_size, reservation_at, status, occasion, notes, phone,
                   duration_minutes, seated_at, completed_at
            FROM reservations r
            JOIN reservation_tables rt ON rt.reservation_id = r.id
            WHERE rt.table_id = ?
              AND r.status IN ('confirmed','seated')
              AND (CASE WHEN r.status='seated' AND seated_at IS NOT NULL THEN r.seated_at ELSE r.reservation_at END)::timestamp <= ?::timestamp
              AND (CASE WHEN r.status='seated' AND seated_at IS NOT NULL THEN r.seated_at ELSE r.reservation_at END)::timestamp
                    + make_interval(mins => r.duration_minutes) > ?::timestamp
            ORDER BY r.reservation_at::timestamp
            LIMIT 1
        """, (table["id"], at.isoformat(timespec="minutes"), at.isoformat(timespec="minutes"))).fetchone()

        item = dict(table)
        item["reservation"] = dict(reservation) if reservation else None
        item["display_status"] = reservation["status"] if reservation else "available"
        if reservation:
            start_value = reservation["seated_at"] if reservation["status"] == "seated" and reservation["seated_at"] else reservation["reservation_at"]
            expected_end = parse_dt(start_value) + timedelta(minutes=reservation["duration_minutes"])
            remaining = int((expected_end - at).total_seconds() // 60)
            item["reservation"]["expected_end"] = expected_end.isoformat(timespec="minutes")
            item["reservation"]["minutes_remaining"] = remaining
            item["reservation"]["timer_state"] = "overdue" if remaining <= 0 else ("ending_soon" if remaining <= 15 else "active")
        result.append(item)

    combinations = conn.execute("""
        SELECT c.id, c.name, c.capacity,
               string_agg(t.name, ', ') AS members
        FROM table_combinations c
        JOIN table_combination_members m ON m.combination_id = c.id
        JOIN restaurant_tables t ON t.id = m.table_id
        WHERE c.area = ?
        GROUP BY c.id, c.name, c.capacity
        ORDER BY c.name
    """, (area,)).fetchall()
    conn.close()

    return jsonify({
        "area": area,
        "at": at.isoformat(timespec="minutes"),
        "tables": result,
        "combinations": [dict(c) for c in combinations],
    })



@app.get("/api/seat-candidates")
def seat_candidates():
    require_admin()
    at_raw = request.args.get("at") or datetime.now().isoformat(timespec="minutes")
    try:
        at = parse_dt(at_raw)
    except ValueError:
        return jsonify({"error": "Invalid date/time."}), 400

    day = at.strftime("%Y-%m-%d")
    conn = db()
    rows = conn.execute("""
        SELECT r.id, r.guest_name, r.party_size, r.reservation_at, r.status,
               r.phone, r.occasion, r.table_id, t.name AS table_name
        FROM reservations r
        LEFT JOIN restaurant_tables t ON t.id = r.table_id
        WHERE r.reservation_at::date = ?::date
          AND r.status = 'confirmed'
        ORDER BY ABS(EXTRACT(EPOCH FROM r.reservation_at::timestamp) - EXTRACT(EPOCH FROM ?::timestamp)),
                 r.reservation_at::timestamp
    """, (day, at.isoformat(timespec="minutes"))).fetchall()
    conn.close()
    return jsonify({"reservations": [dict(r) for r in rows]})


@app.post("/api/tables/<int:table_id>/seat")
def seat_guest_at_table(table_id):
    require_admin()
    reservation_id = request.form.get("reservation_id", "").strip()
    if not reservation_id:
        return jsonify({"error": "Reservation is required."}), 400

    conn = db()
    table = conn.execute(
        "SELECT * FROM restaurant_tables WHERE id = ? AND active = 1",
        (table_id,)
    ).fetchone()
    reservation = conn.execute(
        "SELECT * FROM reservations WHERE id = ?",
        (reservation_id,)
    ).fetchone()

    if not table or not reservation:
        conn.close()
        return jsonify({"error": "Table or reservation not found."}), 404

    if reservation["status"] not in ("confirmed", "seated"):
        conn.close()
        return jsonify({"error": "Only confirmed reservations can be seated."}), 400

    # --- Bar seating: every Bar stool is a fixed capacity-1 seat, so a party of
    # more than one guest can't fit a single stool. Instead of rejecting on
    # capacity, auto-allocate enough free Bar stools (the tapped one plus more)
    # to seat the whole party. Dining and Patio fall through untouched below.
    if table["area"] == "Bar" and reservation["party_size"] > 1:
        bar_tables = conn.execute(
            "SELECT * FROM restaurant_tables WHERE area='Bar' AND active=1 ORDER BY id"
        ).fetchall()

        def _stool_taken(tid):
            # A stool counts as occupied if it's another seated reservation's
            # primary table_id, or one of its allocated reservation_tables rows
            # (multi-stool Bar parties only show up via the latter).
            return conn.execute("""
                SELECT 1 FROM reservations r
                WHERE r.status = 'seated' AND r.id != ? AND (
                    r.table_id = ?
                    OR EXISTS (SELECT 1 FROM reservation_tables rt
                               WHERE rt.reservation_id = r.id AND rt.table_id = ?)
                )
                LIMIT 1
            """, (reservation_id, tid, tid)).fetchone() is not None

        free_stools = [bt for bt in bar_tables if not _stool_taken(bt["id"])]
        free_ids = {bt["id"] for bt in free_stools}
        if table_id not in free_ids:
            conn.close()
            return jsonify({"error": f"Table {table['name']} is no longer available."}), 409

        others = [bt for bt in free_stools if bt["id"] != table_id]
        needed_more = reservation["party_size"] - 1
        if len(others) < needed_more:
            conn.close()
            return jsonify({
                "error": (f"Not enough Bar seats available for this party of "
                          f"{reservation['party_size']}. Only {len(free_stools)} Bar stool(s) are open.")
            }), 409

        chosen = [table] + others[:needed_more]
        chosen_ids = [bt["id"] for bt in chosen]

        duration = recommended_duration("Bar", reservation["party_size"], datetime.now())
        conn.execute("""
            UPDATE reservations
            SET table_id = ?, status = 'seated', seated_at = ?, completed_at = NULL, duration_minutes = ?
            WHERE id = ?
        """, (table_id, datetime.now().isoformat(timespec="minutes"), duration, reservation_id))
        # reservation_tables is the canonical record of every stool this party
        # occupies, so the floor plan shows each one as occupied (see
        # floorplan_data, which already reads occupancy through this table).
        conn.execute("DELETE FROM reservation_tables WHERE reservation_id = ?", (reservation_id,))
        conn.executemany(
            "INSERT INTO reservation_tables(reservation_id, table_id) VALUES (?, ?)",
            [(reservation_id, tid) for tid in chosen_ids]
        )
        conn.commit()
        conn.close()
        stool_names = ", ".join(bt["name"] for bt in chosen)
        return jsonify({"ok": True, "message": f"Guest seated at Bar stools {stool_names}."})

    # --- Unchanged path: Dining, Patio, and single-guest Bar seating.
    if table["capacity"] < reservation["party_size"]:
        conn.close()
        return jsonify({
            "error": f"Table {table['name']} seats {table['capacity']}, but the party has {reservation['party_size']} guests."
        }), 400

    occupied = conn.execute("""
        SELECT id, guest_name
        FROM reservations
        WHERE table_id = ?
          AND status = 'seated'
          AND id != ?
        LIMIT 1
    """, (table_id, reservation_id)).fetchone()

    if occupied:
        conn.close()
        return jsonify({
            "error": f"Table {table['name']} is already occupied by {occupied['guest_name']}."
        }), 409

    # Release any table previously assigned to this reservation and seat at the tapped table.
    duration = recommended_duration(table["area"], reservation["party_size"], datetime.now())
    conn.execute("""
        UPDATE reservations
        SET table_id = ?, status = 'seated', seated_at = ?, completed_at = NULL, duration_minutes = ?
        WHERE id = ?
    """, (table_id, datetime.now().isoformat(timespec="minutes"), duration, reservation_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": f"Guest seated at table {table['name']}."})


@app.post("/api/reservations/<reservation_id>/table-status")
def update_table_status(reservation_id):
    require_admin()
    status = request.form.get("status", "").strip()
    allowed = {"confirmed", "seated", "completed", "cancelled", "no_show"}
    if status not in allowed:
        return jsonify({"error": "Invalid status."}), 400

    conn = db()
    reservation = conn.execute(
        "SELECT * FROM reservations WHERE id = ?",
        (reservation_id,)
    ).fetchone()
    if not reservation:
        conn.close()
        return jsonify({"error": "Reservation not found."}), 404

    if status == "seated":
        table = conn.execute("SELECT area FROM restaurant_tables WHERE id = ?", (reservation["table_id"],)).fetchone()
        duration = recommended_duration(table["area"], reservation["party_size"], datetime.now()) if table else reservation["duration_minutes"]
        conn.execute("UPDATE reservations SET status=?, seated_at=?, completed_at=NULL, duration_minutes=? WHERE id=?",
                     (status, datetime.now().isoformat(timespec="minutes"), duration, reservation_id))
    elif status == "completed":
        conn.execute("UPDATE reservations SET status=?, completed_at=? WHERE id=?",
                     (status, datetime.now().isoformat(timespec="minutes"), reservation_id))
    elif status == "confirmed":
        # A Bar reservation may have multiple stools allocated via reservation_tables
        # (see seat_guest_at_table). Release them all on revert so every stool
        # becomes available again and this reservation can be freshly re-seated.
        # Dining/Patio never write reservation_tables from the seating flow, so
        # this is a no-op for them.
        if reservation["table_id"]:
            table = conn.execute("SELECT area FROM restaurant_tables WHERE id=?", (reservation["table_id"],)).fetchone()
            if table and table["area"] == "Bar":
                conn.execute("DELETE FROM reservation_tables WHERE reservation_id=?", (reservation_id,))
        conn.execute("UPDATE reservations SET status=?, seated_at=NULL, completed_at=NULL WHERE id=?", (status, reservation_id))
    else:
        conn.execute("UPDATE reservations SET status=? WHERE id=?", (status, reservation_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.get("/floorplan")
def floorplan():
    require_admin()
    return render_template(
        "floorplan.html",
        pin=request.args.get("pin"),
        app_name=APP_NAME,
        now=datetime.now().isoformat(timespec="minutes")
    )


@app.post("/admin/assign/<reservation_id>")
def assign_reservation(reservation_id):
    require_admin()
    table_id = request.form.get("table_id", type=int)
    date_str = request.form.get("date")
    conn = db()
    conn.execute("UPDATE reservations SET table_id=? WHERE id=?", (table_id, reservation_id))
    conn.commit()
    conn.close()
    return redirect(url_for("admin", date=date_str, pin=request.form.get("pin")))


@app.post("/admin/reservations/create")
def admin_create_reservation():
    require_admin()
    guest_name = request.form.get("guest_name", "").strip()
    email = request.form.get("email", "").strip() or "host@sienaatl.com"
    phone = request.form.get("phone", "").strip() or "N/A"
    party_size = request.form.get("party_size", type=int)
    reservation_at_raw = request.form.get("reservation_at", "").strip()
    table_id = request.form.get("table_id", type=int)
    status = request.form.get("status", "confirmed")
    occasion = request.form.get("occasion", "").strip()
    notes = request.form.get("notes", "").strip()
    date_str = request.form.get("date")

    if not guest_name or not party_size or not reservation_at_raw:
        flash("Guest name, party size, and reservation time are required.", "error")
        return redirect(url_for("admin", date=date_str, pin=request.form.get("pin")))

    try:
        reservation_at = parse_dt(reservation_at_raw)
    except ValueError:
        flash("Please enter a valid reservation date and time.", "error")
        return redirect(url_for("admin", date=date_str, pin=request.form.get("pin")))

    allowed = {"confirmed", "seated", "completed", "cancelled", "no_show", "walk_in"}
    if status not in allowed:
        status = "confirmed"

    reservation_id = uuid.uuid4().hex[:10].upper()
    conn = db()
    conn.execute("""
        INSERT INTO reservations (
            id, guest_name, email, phone, party_size, reservation_at,
            duration_minutes, table_id, status, occasion, notes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        reservation_id, guest_name, email, phone, party_size,
        reservation_at.isoformat(timespec="minutes"),
        (recommended_duration(conn.execute("SELECT area FROM restaurant_tables WHERE id=?", (table_id,)).fetchone()["area"], party_size, reservation_at) if table_id else TURN_MINUTES),
        table_id, status, occasion, notes,
        datetime.now().isoformat(timespec="seconds")
    ))
    conn.commit()
    conn.close()
    flash(f"Reservation {reservation_id} created.", "success")
    return redirect(url_for("admin", date=reservation_at.strftime("%Y-%m-%d"), pin=request.form.get("pin")))


@app.route("/admin/reservations/<reservation_id>/edit", methods=["GET", "POST"])
def edit_reservation(reservation_id):
    require_admin()
    pin = request.args.get("pin") or request.form.get("pin")
    conn = db()
    reservation = conn.execute(
        "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
    ).fetchone()
    if not reservation:
        conn.close()
        abort(404)

    tables = conn.execute(
        "SELECT * FROM restaurant_tables WHERE active=1 ORDER BY area, name"
    ).fetchall()

    if request.method == "POST":
        guest_name = request.form.get("guest_name", "").strip()
        email = request.form.get("email", "").strip() or "host@sienaatl.com"
        phone = request.form.get("phone", "").strip() or "N/A"
        party_size = request.form.get("party_size", type=int)
        reservation_at_raw = request.form.get("reservation_at", "").strip()
        duration_minutes = request.form.get("duration_minutes", type=int) or TURN_MINUTES
        table_id = request.form.get("table_id", type=int)
        status = request.form.get("status", "confirmed")
        occasion = request.form.get("occasion", "").strip()
        notes = request.form.get("notes", "").strip()

        allowed = {"confirmed", "seated", "completed", "cancelled", "no_show", "walk_in"}
        if not guest_name or not party_size or not reservation_at_raw:
            flash("Guest name, party size, and reservation time are required.", "error")
        elif status not in allowed:
            flash("Please choose a valid reservation status.", "error")
        elif party_size < 1 or party_size > 20:
            flash("Party size must be between 1 and 20.", "error")
        elif duration_minutes < 30 or duration_minutes > 360:
            flash("Dining duration must be between 30 and 360 minutes.", "error")
        else:
            try:
                reservation_at = parse_dt(reservation_at_raw)
            except ValueError:
                flash("Please enter a valid reservation date and time.", "error")
            else:
                # Prevent assigning a table that overlaps another active reservation.
                conflict = None
                if table_id and status in ("confirmed", "seated", "walk_in"):
                    end_at = reservation_at + timedelta(minutes=duration_minutes)
                    conflict = conn.execute("""
                        SELECT id, guest_name, reservation_at
                        FROM reservations
                        WHERE table_id = ?
                          AND id != ?
                          AND status IN ('confirmed','seated','walk_in')
                          AND reservation_at::timestamp < ?::timestamp
                          AND reservation_at::timestamp + make_interval(mins => duration_minutes) > ?::timestamp
                        LIMIT 1
                    """, (
                        table_id, reservation_id,
                        end_at.isoformat(timespec="minutes"),
                        reservation_at.isoformat(timespec="minutes")
                    )).fetchone()

                if conflict:
                    flash(
                        f"That table conflicts with {conflict['guest_name']} at "
                        f"{conflict['reservation_at'].replace('T', ' ')}.",
                        "error"
                    )
                else:
                    # Same Bar-stool release as update_table_status()/update_status():
                    # a manager reverting status to "confirmed" from this edit page
                    # should behave identically to doing it from the floor plan or
                    # admin dashboard. Checked against the table_id from *before*
                    # this edit, since the form can change table_id and status together.
                    if status == "confirmed" and reservation["table_id"]:
                        old_table = conn.execute(
                            "SELECT area FROM restaurant_tables WHERE id=?", (reservation["table_id"],)
                        ).fetchone()
                        if old_table and old_table["area"] == "Bar":
                            conn.execute("DELETE FROM reservation_tables WHERE reservation_id=?", (reservation_id,))
                    conn.execute("""
                        UPDATE reservations
                        SET guest_name=?, email=?, phone=?, party_size=?, reservation_at=?,
                            duration_minutes=?, table_id=?, status=?, occasion=?, notes=?
                        WHERE id=?
                    """, (
                        guest_name, email, phone, party_size,
                        reservation_at.isoformat(timespec="minutes"), duration_minutes,
                        table_id, status, occasion, notes, reservation_id
                    ))
                    conn.commit()
                    conn.close()
                    flash(f"Reservation {reservation_id} updated.", "success")
                    return redirect(url_for(
                        "admin", date=reservation_at.strftime("%Y-%m-%d"), pin=pin
                    ))

        reservation = dict(reservation)
        reservation.update({
            "guest_name": guest_name,
            "email": email,
            "phone": phone,
            "party_size": party_size,
            "reservation_at": reservation_at_raw,
            "duration_minutes": duration_minutes,
            "table_id": table_id,
            "status": status,
            "occasion": occasion,
            "notes": notes,
        })

    conn.close()
    return render_template(
        "edit_reservation.html", reservation=reservation, tables=tables,
        pin=pin, app_name=APP_NAME
    )



def sync_guest_profile(conn, reservation):
    email = (reservation["email"] or "").strip().lower()
    phone = (reservation["phone"] or "").strip()
    existing = conn.execute(
        "SELECT * FROM guest_profiles WHERE lower(COALESCE(email,''))=? AND COALESCE(phone,'')=?",
        (email, phone)
    ).fetchone()
    completed_visits = conn.execute("""
        SELECT COUNT(*) AS n, MAX(COALESCE(completed_at, reservation_at)) AS last_visit
        FROM reservations
        WHERE lower(COALESCE(email,''))=? AND COALESCE(phone,'')=?
          AND status='completed'
    """, (email, phone)).fetchone()
    if existing:
        conn.execute("""
            UPDATE guest_profiles SET guest_name=?, visit_count=?, last_visit_at=?
            WHERE id=?
        """, (reservation["guest_name"], completed_visits["n"], completed_visits["last_visit"], existing["id"]))
    else:
        conn.execute("""
            INSERT INTO guest_profiles(email, phone, guest_name, visit_count, last_visit_at)
            VALUES (?, ?, ?, ?, ?)
        """, (email, phone, reservation["guest_name"], completed_visits["n"], completed_visits["last_visit"]))


@app.get("/timeline")
def timeline():
    require_admin()
    date_str = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")
    conn = db()
    reservations = conn.execute("""
        SELECT r.*, t.name AS table_name, t.area
        FROM reservations r
        LEFT JOIN restaurant_tables t ON t.id=r.table_id
        WHERE r.reservation_at::date=?::date
          AND r.status NOT IN ('cancelled','no_show')
        ORDER BY r.reservation_at::timestamp, t.name
    """, (date_str,)).fetchall()
    tables = conn.execute("""
        SELECT t.*, s.name AS server_name
        FROM restaurant_tables t
        LEFT JOIN servers s ON s.id=t.server_id
        WHERE t.active=1 ORDER BY t.area, t.name
    """).fetchall()
    conn.close()

    start = datetime.strptime(date_str + f" {OPEN_HOUR:02d}:00", "%Y-%m-%d %H:%M")
    slots = [(start + timedelta(minutes=30*i)).strftime("%H:%M") for i in range((CLOSE_HOUR-OPEN_HOUR)*2+1)]
    return render_template("timeline.html", reservations=reservations, tables=tables,
                           slots=slots, date_str=date_str, pin=request.args.get("pin"),
                           app_name=APP_NAME)


@app.route("/waitlist", methods=["GET", "POST"])
def waitlist_page():
    require_admin()
    pin = request.values.get("pin")
    conn = db()
    if request.method == "POST":
        action = request.form.get("action", "add")
        if action == "add":
            guest_name = request.form.get("guest_name", "").strip()
            phone = request.form.get("phone", "").strip()
            party_size = request.form.get("party_size", type=int)
            quoted = request.form.get("quoted_minutes", type=int) or 30
            area = request.form.get("area_preference", "").strip()
            notes = request.form.get("notes", "").strip()
            if not guest_name or not phone or not party_size:
                conn.close()
                flash("Name, phone, and party size are required.", "error")
                return redirect(url_for("waitlist_page", pin=pin))
            conn.execute("""
                INSERT INTO waitlist(id, guest_name, phone, party_size, area_preference,
                                     quoted_minutes, notes, joined_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (uuid.uuid4().hex[:10].upper(), guest_name, phone, party_size, area,
                  quoted, notes, datetime.now().isoformat(timespec="minutes")))
            conn.commit()
        elif action in {"notify", "remove"}:
            wait_id = request.form.get("wait_id")
            if action == "notify":
                item = conn.execute("SELECT * FROM waitlist WHERE id=?", (wait_id,)).fetchone()
                if item:
                    text = (f"Hi {item['guest_name'].split()[0]}, your table at Siena is ready. "
                            f"Please check in with the host within 10 minutes. {RESTAURANT_ADDRESS}")
                    if send_sms(item["phone"], text):
                        flash("Table-ready text sent.", "success")
                    else:
                        flash("Guest marked notified, but SMS could not be sent.", "error")
                conn.execute("UPDATE waitlist SET status='notified', notified_at=? WHERE id=?",
                             (datetime.now().isoformat(timespec="minutes"), wait_id))
            else:
                conn.execute("UPDATE waitlist SET status='removed' WHERE id=?", (wait_id,))
            conn.commit()
        elif action == "seat":
            wait_id = request.form.get("wait_id")
            table_id = request.form.get("table_id", type=int)
            item = conn.execute("SELECT * FROM waitlist WHERE id=?", (wait_id,)).fetchone()
            table = conn.execute("SELECT * FROM restaurant_tables WHERE id=?", (table_id,)).fetchone()
            if item and table and table["capacity"] >= item["party_size"]:
                reservation_id = uuid.uuid4().hex[:10].upper()
                duration = recommended_duration(table["area"], item["party_size"], datetime.now())
                now_value = datetime.now().isoformat(timespec="minutes")
                conn.execute("""
                    INSERT INTO reservations(
                        id, guest_name, email, phone, party_size, reservation_at,
                        duration_minutes, table_id, status, occasion, notes,
                        manage_token, seated_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'seated', '', ?, ?, ?, ?)
                """, (reservation_id, item["guest_name"], "walkin@sienaatl.com", item["phone"],
                      item["party_size"], now_value, duration, table_id, item["notes"],
                      secrets.token_urlsafe(32), now_value, now_value))
                conn.execute("""
                    UPDATE waitlist SET status='seated', seated_at=?, reservation_id=? WHERE id=?
                """, (now_value, reservation_id, wait_id))
                conn.commit()
            else:
                flash("The selected table does not fit this party.", "error")
        conn.close()
        return redirect(url_for("waitlist_page", pin=pin))

    items = conn.execute("""
        SELECT * FROM waitlist
        WHERE status IN ('waiting','notified')
        ORDER BY joined_at
    """).fetchall()
    tables = conn.execute("""
        SELECT t.* FROM restaurant_tables t
        WHERE t.active=1
          AND NOT EXISTS (
            SELECT 1 FROM reservations r
            WHERE r.table_id=t.id AND r.status='seated'
          )
        ORDER BY t.area, t.capacity, t.name
    """).fetchall()
    conn.close()
    return render_template("waitlist.html", items=items, tables=tables, pin=pin,
                           app_name=APP_NAME, now=datetime.now())


@app.route("/servers", methods=["GET", "POST"])
def servers_page():
    require_admin()
    pin = request.values.get("pin")
    conn = db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name", "").strip()
            if name:
                conn.execute("INSERT INTO servers(name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,))
        elif action == "assign":
            table_id = request.form.get("table_id", type=int)
            server_id = request.form.get("server_id", type=int)
            conn.execute("UPDATE restaurant_tables SET server_id=? WHERE id=?", (server_id or None, table_id))
        conn.commit()
        conn.close()
        return redirect(url_for("servers_page", pin=pin))

    servers = conn.execute("SELECT * FROM servers WHERE active=1 ORDER BY name").fetchall()
    tables = conn.execute("""
        SELECT t.*, s.name AS server_name
        FROM restaurant_tables t LEFT JOIN servers s ON s.id=t.server_id
        WHERE t.active=1 ORDER BY t.area, t.name
    """).fetchall()
    today = datetime.now().strftime("%Y-%m-%d")
    loads = conn.execute("""
        SELECT s.id, s.name,
               COUNT(DISTINCT r.id) AS parties,
               COALESCE(SUM(r.party_size),0) AS covers
        FROM servers s
        LEFT JOIN restaurant_tables t ON t.server_id=s.id
        LEFT JOIN reservations r ON r.table_id=t.id
          AND r.reservation_at::date=?::date
          AND r.status IN ('confirmed','seated','completed','walk_in')
        WHERE s.active=1
        GROUP BY s.id, s.name ORDER BY s.name
    """, (today,)).fetchall()
    conn.close()
    return render_template("servers.html", servers=servers, tables=tables, loads=loads,
                           pin=pin, app_name=APP_NAME)


@app.route("/guests", methods=["GET", "POST"])
def guests_page():
    require_admin()
    pin = request.values.get("pin")
    conn = db()
    if request.method == "POST":
        profile_id = request.form.get("profile_id", type=int)
        conn.execute("""
            UPDATE guest_profiles SET vip=?, allergies=?, preferences=?,
                favorite_table=?, preferred_server=?, birthday=?, marketing_opt_in=?,
                marketing_opt_out_at=CASE WHEN ?=1 THEN NULL ELSE marketing_opt_out_at END WHERE id=?
        """, (1 if request.form.get("vip") else 0,
              request.form.get("allergies","").strip(),
              request.form.get("preferences","").strip(),
              request.form.get("favorite_table","").strip(),
              request.form.get("preferred_server","").strip(),
              request.form.get("birthday","").strip() or None,
              1 if request.form.get("marketing_opt_in") else 0,
              1 if request.form.get("marketing_opt_in") else 0,
              profile_id))
        conn.commit()
        conn.close()
        return redirect(url_for("guests_page", pin=pin))

    recent = conn.execute("SELECT * FROM reservations ORDER BY created_at DESC").fetchall()
    for r in recent:
        sync_guest_profile(conn, r)
    conn.commit()
    q = request.args.get("q", "").strip()
    sql = "SELECT * FROM guest_profiles"
    params = []
    if q:
        sql += " WHERE lower(guest_name) LIKE ? OR lower(COALESCE(email,'')) LIKE ? OR COALESCE(phone,'') LIKE ?"
        needle = f"%{q.lower()}%"
        params = [needle, needle, needle]
    sql += " ORDER BY vip DESC, visit_count DESC, guest_name"
    guests = conn.execute(sql, params).fetchall()
    conn.close()
    return render_template("guests.html", guests=guests, q=q, pin=pin, app_name=APP_NAME)


@app.get("/manager")
def manager_overview():
    require_admin()
    date_str = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")
    conn = db()
    stats = conn.execute("""
        SELECT COUNT(*) bookings,
               COALESCE(SUM(CASE WHEN status NOT IN ('cancelled','no_show') THEN party_size ELSE 0 END),0) covers,
               SUM(CASE WHEN status='seated' THEN 1 ELSE 0 END) seated,
               SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END) upcoming,
               SUM(CASE WHEN status='no_show' THEN 1 ELSE 0 END) no_shows
        FROM reservations WHERE reservation_at::date=?::date
    """, (date_str,)).fetchone()
    waiting = conn.execute("SELECT COUNT(*) n FROM waitlist WHERE status IN ('waiting','notified')").fetchone()["n"]
    now_param = datetime.now().isoformat(timespec="minutes")
    overdue = conn.execute("""
        SELECT r.guest_name, r.party_size, r.seated_at, r.duration_minutes, t.name table_name
        FROM reservations r JOIN restaurant_tables t ON t.id=r.table_id
        WHERE r.status='seated'
          AND r.seated_at::timestamp + make_interval(mins => r.duration_minutes) < ?::timestamp
        ORDER BY r.seated_at::timestamp
    """, (now_param,)).fetchall()
    upcoming_large = conn.execute("""
        SELECT r.*, t.name table_name FROM reservations r
        LEFT JOIN restaurant_tables t ON t.id=r.table_id
        WHERE r.reservation_at::date=?::date AND r.party_size>=6
          AND r.status IN ('confirmed','seated')
        ORDER BY r.reservation_at::timestamp
    """, (date_str,)).fetchall()
    conn.close()
    return render_template("manager.html", stats=stats, waiting=waiting, overdue=overdue,
                           upcoming_large=upcoming_large, date_str=date_str,
                           pin=request.args.get("pin"), app_name=APP_NAME)



@app.route("/marketing", methods=["GET", "POST"])
def marketing_page():
    require_admin()
    pin = request.values.get("pin")
    conn = db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        message = request.form.get("message", "").strip()
        audience = request.form.get("audience", "all")
        if not name or not message:
            conn.close(); flash("Campaign name and message are required.", "error")
            return redirect(url_for("marketing_page", pin=pin))
        if "STOP" not in message.upper():
            message += " Reply STOP to opt out."
        sql = "SELECT * FROM guest_profiles WHERE marketing_opt_in=1 AND marketing_opt_out_at IS NULL AND phone IS NOT NULL AND phone != ''"
        params = []
        if audience == "vip": sql += " AND vip=1"
        elif audience == "birthday": sql += " AND to_char(birthday::date, 'MM')=?"; params=[datetime.now().strftime('%m')]
        recipients = conn.execute(sql, params).fetchall()
        sent = 0
        for guest in recipients:
            personalized = message.replace("{first_name}", guest["guest_name"].split()[0])
            if send_sms(guest["phone"], personalized): sent += 1
        campaign_id = uuid.uuid4().hex[:10].upper()
        conn.execute("INSERT INTO sms_campaigns(id,name,message,audience,sent_at,recipient_count,created_at) VALUES (?,?,?,?,?,?,?)",
                     (campaign_id,name,message,audience,datetime.now().isoformat(timespec="seconds"),sent,datetime.now().isoformat(timespec="seconds")))
        conn.commit(); conn.close()
        flash(f"Campaign sent to {sent} opted-in guests.", "success")
        return redirect(url_for("marketing_page", pin=pin))
    campaigns = conn.execute("SELECT * FROM sms_campaigns ORDER BY created_at DESC LIMIT 30").fetchall()
    opted_in = conn.execute("SELECT COUNT(*) n FROM guest_profiles WHERE marketing_opt_in=1 AND marketing_opt_out_at IS NULL").fetchone()["n"]
    conn.close()
    return render_template("marketing.html", campaigns=campaigns, opted_in=opted_in, pin=pin, app_name=APP_NAME)

@app.get("/admin")
def admin():
    require_admin()
    date_str = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")
    status_filter = request.args.get("status", "all")
    search = request.args.get("search", "").strip()
    conn = db()

    query = """
        SELECT r.*, t.name AS table_name, t.area
        FROM reservations r
        LEFT JOIN restaurant_tables t ON t.id = r.table_id
        WHERE r.reservation_at::date = ?::date
    """
    params = [date_str]
    if status_filter != "all":
        query += " AND r.status = ?"
        params.append(status_filter)
    if search:
        query += " AND (lower(r.guest_name) LIKE ? OR lower(r.phone) LIKE ? OR lower(r.email) LIKE ? OR lower(r.id) LIKE ?)"
        needle = f"%{search.lower()}%"
        params.extend([needle, needle, needle, needle])
    query += " ORDER BY r.reservation_at::timestamp"

    reservations = conn.execute(query, params).fetchall()
    all_day = conn.execute("""
        SELECT r.*, t.name AS table_name, t.area
        FROM reservations r
        LEFT JOIN restaurant_tables t ON t.id = r.table_id
        WHERE r.reservation_at::date = ?::date
        ORDER BY r.reservation_at::timestamp
    """, (date_str,)).fetchall()
    tables = conn.execute("SELECT * FROM restaurant_tables WHERE active=1 ORDER BY area, name").fetchall()
    area_counts = conn.execute("""
        SELECT t.area,
               COUNT(DISTINCT t.id) AS table_count,
               SUM(CASE WHEN r.status IN ('confirmed','seated','walk_in') THEN 1 ELSE 0 END) AS occupied
        FROM restaurant_tables t
        LEFT JOIN reservations r ON r.table_id=t.id AND r.reservation_at::date=?::date
        WHERE t.active=1
        GROUP BY t.area
        ORDER BY t.area
    """, (date_str,)).fetchall()
    conn.close()

    active = [r for r in all_day if r["status"] not in ("cancelled", "no_show")]
    stats = {
        "covers": sum(r["party_size"] for r in active),
        "bookings": len(active),
        "seated": sum(1 for r in all_day if r["status"] == "seated"),
        "confirmed": sum(1 for r in all_day if r["status"] == "confirmed"),
        "cancelled": sum(1 for r in all_day if r["status"] == "cancelled"),
        "no_show": sum(1 for r in all_day if r["status"] == "no_show"),
        "walk_in": sum(1 for r in all_day if r["status"] == "walk_in"),
        "unassigned": sum(1 for r in active if not r["table_id"]),
    }
    total_capacity = sum(t["capacity"] for t in tables)
    stats["occupancy"] = round((stats["covers"] / total_capacity) * 100) if total_capacity else 0

    return render_template(
        "admin.html", reservations=reservations, tables=tables,
        date_str=date_str, stats=stats, pin=request.args.get("pin"),
        app_name=APP_NAME, status_filter=status_filter, search=search,
        area_counts=area_counts,
        now_local=datetime.now().isoformat(timespec="minutes")
    )


@app.post("/admin/status/<reservation_id>")
def update_status(reservation_id):
    require_admin()
    status = request.form.get("status")
    allowed = {"confirmed", "seated", "completed", "cancelled", "no_show", "walk_in"}
    if status not in allowed:
        abort(400)
    conn = db()
    if status == "confirmed":
        # Same Bar-stool release as update_table_status(): this dashboard dropdown
        # is a second path that can also revert Seated -> Confirmed, so it needs
        # the same reservation_tables cleanup to avoid stale stool assignments.
        reservation = conn.execute("SELECT table_id FROM reservations WHERE id=?", (reservation_id,)).fetchone()
        if reservation and reservation["table_id"]:
            table = conn.execute("SELECT area FROM restaurant_tables WHERE id=?", (reservation["table_id"],)).fetchone()
            if table and table["area"] == "Bar":
                conn.execute("DELETE FROM reservation_tables WHERE reservation_id=?", (reservation_id,))
    conn.execute("UPDATE reservations SET status = ? WHERE id = ?", (status, reservation_id))
    conn.commit()
    conn.close()
    audit("update_reservation_status", "reservation", reservation_id, {"status": status})
    return redirect(url_for("admin", date=request.form.get("date"), pin=request.form.get("pin")))


@app.post("/admin/tables")
def add_table():
    require_admin()
    name = request.form.get("name", "").strip()
    capacity = request.form.get("capacity", type=int)
    area = request.form.get("area", "").strip() or "Covered Patio"
    if not name or not capacity:
        abort(400)
    conn = db()
    conn.execute(
        "INSERT INTO restaurant_tables(name, capacity, area) VALUES (?, ?, ?)",
        (name, capacity, area)
    )
    conn.commit()
    conn.close()
    return redirect(url_for("admin", pin=request.form.get("pin")))


# Initialize the schema when the application is imported by Gunicorn.
init_db()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
