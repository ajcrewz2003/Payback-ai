from contextlib import asynccontextmanager
import csv
import io
import json
import os
import sqlite3
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import smtplib

from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openai import OpenAI
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ.get("DATABASE_URL")


def get_db_connection():
  if DATABASE_URL:
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn
  else:
    conn = sqlite3.connect("payback.db")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
  conn = get_db_connection()
  cursor = conn.cursor()

  if DATABASE_URL:
    cursor.execute(
        """
            CREATE TABLE IF NOT EXISTS invoices (
                id SERIAL PRIMARY KEY,
                invoice_id TEXT UNIQUE NOT NULL,
                client_name TEXT NOT NULL,
                amount REAL NOT NULL,
                due_date TEXT,
                status TEXT DEFAULT 'FRIENDLY'
            )
            """
    )
  else:
    cursor.execute(
        """
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id TEXT UNIQUE NOT NULL,
                client_name TEXT NOT NULL,
                amount REAL NOT NULL,
                due_date TEXT,
                status TEXT DEFAULT 'FRIENDLY'
            )
            """
    )
  conn.commit()
  conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
  init_db()
  yield


app = FastAPI(title="Payback AI", lifespan=lifespan)

if os.path.exists("static"):
  app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

# --- ROUTES ---


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
  conn = get_db_connection()
  invoices = conn.execute("SELECT * FROM invoices ORDER BY id DESC").fetchall()
  conn.close()

  total_outstanding = sum(
      inv["amount"] for inv in invoices if inv["status"] != "PAID"
  )
  total_invoices = len(invoices)
  urgent_notices = sum(1 for inv in invoices if inv["status"] == "URGENT")

  stats = {
      "total_outstanding": total_outstanding,
      "total_invoices": total_invoices,
      "urgent_notices": urgent_notices,
  }

  return templates.TemplateResponse(
      request=request,
      name="index.html",
      context={"invoices": invoices, "stats": stats},
  )


@app.post("/add-invoice")
@app.post("/api/invoices")
@app.post("/add")
async def add_invoice(request: Request):
  conn = get_db_connection()
  cursor = conn.cursor()

  content_type = request.headers.get("content-type", "")

  if "application/json" in content_type:
    data = await request.json()
    inv_id = data.get("invoice_id")
    client = data.get("client_name")
    amount = float(data.get("amount", 0))
    due_date = data.get("due_date", "")
    status = data.get("status", "FRIENDLY")
  else:
    form = await request.form()
    inv_id = form.get("invoice_id")
    client = form.get("client_name")
    amount = float(form.get("amount", 0))
    due_date = form.get("due_date", "")
    status = form.get("status", "FRIENDLY")

  try:
    cursor.execute(
        """
            INSERT INTO invoices (invoice_id, client_name, amount, due_date, status)
            VALUES (?, ?, ?, ?, ?)
        """
        if not DATABASE_URL
        else """
            INSERT INTO invoices (invoice_id, client_name, amount, due_date, status)
            VALUES (%s, %s, %s, %s, %s)
        """,
        (inv_id, client, amount, due_date, status),
    )
    conn.commit()
  except Exception:
    conn.close()
    return JSONResponse(
        {"status": "error", "message": "Invoice ID already exists or database error"},
        status_code=400,
    )
  finally:
    conn.close()

  return JSONResponse(
      {"status": "success", "message": "Invoice created successfully"}
  )


# --- OPENAI PARSER ENDPOINT ---


@app.post("/api/ai-add")
async def ai_add_invoice(request: Request):
  api_key = os.environ.get("OPENAI_API_KEY")
  if not api_key:
    return JSONResponse(
        {
            "status": "error",
            "message": "OPENAI_API_KEY environment variable is missing on Render.",
        },
        status_code=500,
    )

  data = await request.json()
  user_prompt = data.get("prompt", "")

  if not user_prompt:
    return JSONResponse(
        {"status": "error", "message": "Prompt cannot be empty"}, status_code=400
    )

  client = OpenAI(api_key=api_key)
  current_date_str = datetime.now().strftime("%Y-%m-%d (%A)")

  system_instruction = (
      "Extract invoice details from user text. Return structured JSON with"
      " keys:\n- invoice_id: string (generate short unique code like Inv_101)\n-"
      " client_name: string (person or company name)\n- amount: number (float"
      " value)\n- due_date: string (YYYY-MM-DD or readable date string)\n-"
      " status: string (must be exactly 'FRIENDLY', 'URGENT', or"
      f" 'PAID')\n\nCRITICAL CONTEXT: The current active calendar date is"
      f" {current_date_str}. Always calculate any relative dates (like 'next"
      f" week', 'tomorrow', or 'in 5 days') relative to this current date"
      f" ({current_date_str})."
  )
  try:
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
    )

    extracted = json.loads(response.choices[0].message.content)

    inv_id = extracted.get("invoice_id", "Inv_AI")
    client_name = extracted.get("client_name", "Unknown")
    amount = float(extracted.get("amount", 0))
    due_date = extracted.get("due_date", "")
    status = extracted.get("status", "FRIENDLY").upper()
    if status not in ["FRIENDLY", "URGENT", "PAID"]:
      status = "FRIENDLY"

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
            INSERT INTO invoices (invoice_id, client_name, amount, due_date, status)
            VALUES (?, ?, ?, ?, ?)
        """
        if not DATABASE_URL
        else """
            INSERT INTO invoices (invoice_id, client_name, amount, due_date, status)
            VALUES (%s, %s, %s, %s, %s)
        """,
        (inv_id, client_name, amount, due_date, status),
    )
    conn.commit()
    conn.close()

    return JSONResponse(
        {"status": "success", "message": "AI invoice created successfully!"}
    )

  except Exception as e:
    return JSONResponse(
        {"status": "error", "message": f"AI Parsing or DB failed: {str(e)}"},
        status_code=500,
    )


@app.get("/api/export-csv")
async def export_csv():
  conn = get_db_connection()
  invoices = conn.execute(
      "SELECT invoice_id, client_name, amount, due_date, status FROM invoices"
  ).fetchall()
  conn.close()

  output = io.StringIO()
  writer = csv.writer(output)
  writer.writerow(["Invoice ID", "Client Name", "Amount", "Due Date", "Status"])

  for inv in invoices:
    writer.writerow([
        inv["invoice_id"],
        inv["client_name"],
        inv["amount"],
        inv["due_date"],
        inv["status"],
    ])

  output.seek(0)
  return StreamingResponse(
      iter([output.getvalue()]),
      media_type="text/csv",
      headers={"Content-Disposition": "attachment; filename=invoices_export.csv"},
  )


@app.get("/api/reminder-email/{invoice_id}")
async def get_reminder_email(invoice_id: str):
  conn = get_db_connection()
  cursor = conn.cursor()
  cursor.execute(
      "SELECT * FROM invoices WHERE invoice_id = ? OR id = ?"
      if not DATABASE_URL
      else "SELECT * FROM invoices WHERE invoice_id = %s OR id::text = %s",
      (invoice_id, invoice_id),
  )
  inv = cursor.fetchone()
  conn.close()

  if not inv:
    return JSONResponse(
        {"status": "error", "message": "Invoice not found"}, status_code=404
    )

  subject = f"Friendly Reminder: Invoice {inv['invoice_id']} for ${inv['amount']}"
  body = (
      f"Hi {inv['client_name']},\n\nI hope you're doing well! This is a quick"
      f" note to remind you about invoice {inv['invoice_id']} for"
      f" ${inv['amount']:.2f}, which was due on {inv['due_date']}.\n\nPlease"
      " let me know when you might be able to process this payment. Thank"
      " you so much!\n\nBest regards,\nYour Name"
  )

  return {"subject": subject, "body": body}


@app.post("/update-status/{invoice_id}")
async def update_status(invoice_id: str, request: Request):
  data = await request.json()
  new_status = data.get("status", "FRIENDLY")

  conn = get_db_connection()
  cursor = conn.cursor()

  cursor.execute(
      "UPDATE invoices SET status = ? WHERE invoice_id = ?"
      if not DATABASE_URL
      else "UPDATE invoices SET status = %s WHERE invoice_id = %s",
      (new_status, invoice_id),
  )
  if cursor.rowcount == 0:
    cursor.execute(
        "UPDATE invoices SET status = ? WHERE id = ?"
        if not DATABASE_URL
        else "UPDATE invoices SET status = %s WHERE id::text = %s",
        (new_status, invoice_id),
    )

  conn.commit()
  conn.close()

  return JSONResponse(
      {"status": "success", "message": "Status updated successfully"}
  )


@app.post("/delete-invoice/{invoice_id}")
@app.post("/delete/{invoice_id}")
@app.delete("/api/invoices/{invoice_id}")
async def delete_invoice(invoice_id: str):
  conn = get_db_connection()
  cursor = conn.cursor()

  cursor.execute(
      "DELETE FROM invoices WHERE invoice_id = ?"
      if not DATABASE_URL
      else "DELETE FROM invoices WHERE invoice_id = %s",
      (invoice_id,),
  )
  if cursor.rowcount == 0:
    cursor.execute(
        "DELETE FROM invoices WHERE id = ?"
        if not DATABASE_URL
        else "DELETE FROM invoices WHERE id::text = %s",
        (invoice_id,),
    )

  conn.commit()
  conn.close()

  return JSONResponse(
      {"status": "success", "message": "Invoice deleted successfully"}
  )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
  return templates.TemplateResponse(
      request=request, name="login.html", context={"request": request}
  )


@app.post("/login")
async def login_action(request: Request):
  form = await request.form()
  username = form.get("username")
  password = form.get("password")

  if username == "admin" and password == "admin":
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(key="session_user", value=str(username))
    return response
  return templates.TemplateResponse(
      request=request,
      name="login.html",
      context={"request": request, "error": "Invalid credentials"},
  )


@app.get("/logout")
async def logout():
  response = RedirectResponse(url="/login", status_code=303)
  response.delete_cookie(key="session_user")
  return response


@app.post("/api/send-email/{invoice_id}")
async def send_invoice_email(invoice_id: str, request: Request):
  data = await request.json()
  recipient_email = data.get("email")

  if not recipient_email:
    return JSONResponse(
        {"status": "error", "message": "Recipient email is required"},
        status_code=400,
    )

  conn = get_db_connection()
  cursor = conn.cursor()
  cursor.execute(
      "SELECT * FROM invoices WHERE invoice_id = ? OR id = ?"
      if not DATABASE_URL
      else "SELECT * FROM invoices WHERE invoice_id = %s OR id::text = %s",
      (invoice_id, invoice_id),
  )
  inv = cursor.fetchone()
  conn.close()

  if not inv:
    return JSONResponse(
        {"status": "error", "message": "Invoice not found"}, status_code=404
    )

  smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
  smtp_port = int(os.environ.get("SMTP_PORT", 587))
  sender_email = os.environ.get("SMTP_USER")
  sender_password = os.environ.get("SMTP_PASSWORD")

  if not sender_email or not sender_password:
    return JSONResponse(
        {
            "status": "error",
            "message": "SMTP credentials are not configured on the server.",
        },
        status_code=500,
    )

  subject = f"Friendly Reminder: Invoice {inv['invoice_id']} for ${inv['amount']}"
  body = (
      f"Hi {inv['client_name']},\n\nI hope you're doing well! This is a quick"
      f" note to remind you about invoice {inv['invoice_id']} for"
      f" ${inv['amount']:.2f}, which was due on {inv['due_date']}.\n\nPlease"
      " let me know when you might be able to process this payment. Thank"
      " you so much!\n\nBest regards,\nYour Name"
  )

  msg = MIMEMultipart()
  msg["From"] = sender_email
  msg["To"] = recipient_email
  msg["Subject"] = subject
  msg.attach(MIMEText(body, "plain"))

  try:
    server = smtplib.SMTP(smtp_server, smtp_port)
    server.starttls()
    server.login(sender_email, sender_password)
    server.sendmail(sender_email, recipient_email, msg.as_string())
    server.quit()
    return {"status": "success", "message": "Email sent successfully!"}
  except Exception as e:
    return JSONResponse(
        {"status": "error", "message": f"Failed to send email: {str(e)}"},
        status_code=500,
    )