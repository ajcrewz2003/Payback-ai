from contextlib import asynccontextmanager
import csv
import io
import json
import os
import random
import sqlite3
from datetime import datetime
import resend

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
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        if DATABASE_URL:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS invoices (
                    id SERIAL PRIMARY KEY,
                    invoice_id TEXT UNIQUE NOT NULL,
                    client_name TEXT NOT NULL,
                    amount DOUBLE PRECISION NOT NULL,
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
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"DB Init Error: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="Payback AI", lifespan=lifespan)

if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM invoices ORDER BY id DESC")
        invoices = cursor.fetchall()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Home route DB error: {e}")
        invoices = []

    total_outstanding = sum(
        float(inv["amount"]) for inv in invoices if inv["status"] != "PAID"
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
        conn = get_db_connection()
        cursor = conn.cursor()
        if DATABASE_URL:
            cursor.execute(
                "INSERT INTO invoices (invoice_id, client_name, amount, due_date, status) VALUES (%s, %s, %s, %s, %s)",
                (inv_id, client, amount, due_date, status),
            )
        else:
            cursor.execute(
                "INSERT INTO invoices (invoice_id, client_name, amount, due_date, status) VALUES (?, ?, ?, ?, ?)",
                (inv_id, client, amount, due_date, status),
            )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        return JSONResponse({"success": False, "status": "error", "message": str(e)}, status_code=400)

    return JSONResponse({"success": True, "status": "success", "message": "Invoice created successfully"})

@app.post("/api/ai-add")
async def ai_add_invoice(request: Request):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return JSONResponse({"success": False, "status": "error", "message": "OPENAI_API_KEY is missing."}, status_code=500)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"success": False, "status": "error", "message": "Invalid JSON payload"}, status_code=400)

    user_prompt = data.get("prompt", "")
    if not user_prompt:
        return JSONResponse({"success": False, "status": "error", "message": "Prompt cannot be empty"}, status_code=400)

    try:
        client = OpenAI(api_key=api_key)
        current_date_str = datetime.now().strftime("%Y-%m-%d (%A)")

        system_instruction = (
            "Extract invoice details from user text. Return structured JSON with keys:\n"
            "- invoice_id: string\n"
            "- client_name: string (person or company name)\n"
            "- amount: number (float value)\n"
            "- due_date: string (YYYY-MM-DD or readable date string)\n"
            "- status: string (must be exactly 'FRIENDLY', 'URGENT', or 'PAID')\n\n"
            f"CRITICAL CONTEXT: The current active calendar date is {current_date_str}."
        )

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )
        extracted = json.loads(response.choices[0].message.content)
        
        inv_id = f"Inv_{random.randint(10000, 99999)}"
            
        client_name = extracted.get("client_name", "Unknown")
        amount = float(extracted.get("amount", 0))
        due_date = extracted.get("due_date", "")
        status = extracted.get("status", "FRIENDLY").upper()
        if status not in ["FRIENDLY", "URGENT", "PAID"]:
            status = "FRIENDLY"

        conn = get_db_connection()
        cursor = conn.cursor()
        if DATABASE_URL:
            cursor.execute(
                "INSERT INTO invoices (invoice_id, client_name, amount, due_date, status) VALUES (%s, %s, %s, %s, %s)",
                (inv_id, client_name, amount, due_date, status),
            )
        else:
            cursor.execute(
                "INSERT INTO invoices (invoice_id, client_name, amount, due_date, status) VALUES (?, ?, ?, ?, ?)",
                (inv_id, client_name, amount, due_date, status),
            )
        conn.commit()
        cursor.close()
        conn.close()
        return JSONResponse({"success": True, "status": "success", "message": "AI invoice created successfully!"})
    except Exception as e:
        print(f"AI Add Error Details: {str(e)}")
        return JSONResponse({"success": False, "status": "error", "message": str(e)}, status_code=500)

@app.get("/api/export-csv")
async def export_csv():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT invoice_id, client_name, amount, due_date, status FROM invoices")
    invoices = cursor.fetchall()
    cursor.close()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Invoice ID", "Client Name", "Amount", "Due Date", "Status"])
    for inv in invoices:
        writer.writerow([inv["invoice_id"], inv["client_name"], inv["amount"], inv["due_date"], inv["status"]])

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
    if DATABASE_URL:
        cursor.execute("SELECT * FROM invoices WHERE invoice_id = %s OR id::text = %s", (invoice_id, invoice_id))
    else:
        cursor.execute("SELECT * FROM invoices WHERE invoice_id = ? OR id = ?", (invoice_id, invoice_id))
    inv = cursor.fetchone()
    cursor.close()
    conn.close()

    if not inv:
        return JSONResponse({"success": False, "status": "error", "message": "Invoice not found"}, status_code=404)

    subject = f"Friendly Reminder: Invoice {inv['invoice_id']} for ${inv['amount']}"
    body = (
        f"Hi {inv['client_name']},\n\nI hope you're doing well! This is a quick note to remind you about invoice "
        f"{inv['invoice_id']} for ${float(inv['amount']):.2f}, which was due on {inv['due_date']}.\n\n"
        f"Please let me know when you might be able to process this payment. Thank you so much!\n\nBest regards,\nYour Name"
    )
    return {"subject": subject, "body": body}

@app.post("/update-status/{invoice_id}")
@app.post("/api/invoices/{invoice_id}/status")
async def update_status(invoice_id: str, request: Request):
    data = await request.json()
    new_status = data.get("status", "FRIENDLY").upper()
    conn = get_db_connection()
    cursor = conn.cursor()
    if DATABASE_URL:
        cursor.execute("UPDATE invoices SET status = %s WHERE invoice_id = %s OR id::text = %s", (new_status, invoice_id, invoice_id))
    else:
        cursor.execute("UPDATE invoices SET status = ? WHERE invoice_id = ? OR id = ?", (new_status, invoice_id, invoice_id))
    conn.commit()
    cursor.close()
    conn.close()
    return JSONResponse({"success": True, "status": "success", "message": "Status updated successfully"})

@app.post("/delete-invoice/{invoice_id}")
@app.post("/delete/{invoice_id}")
@app.delete("/api/invoices/{invoice_id}")
async def delete_invoice(invoice_id: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    if DATABASE_URL:
        cursor.execute("DELETE FROM invoices WHERE invoice_id = %s OR id::text = %s", (invoice_id, invoice_id))
    else:
        cursor.execute("DELETE FROM invoices WHERE invoice_id = ? OR id = ?", (invoice_id, invoice_id))
    conn.commit()
    cursor.close()
    conn.close()
    return JSONResponse({"success": True, "status": "success", "message": "Invoice deleted successfully"})

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"request": request})

@app.post("/login")
async def login_action(request: Request):
    form = await request.form()
    username = form.get("username")
    password = form.get("password")
    if username == "admin" and password == "admin":
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(key="session_user", value=str(username))
        return response
    return templates.TemplateResponse(request=request, name="login.html", context={"request": request, "error": "Invalid credentials"})

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key="session_user")
    return response

@app.post("/api/send-email/{invoice_id}")
async def send_reminder_email(invoice_id: str, request: Request):
    try:
        data = await request.json()
        client_email = data.get("email")
        
        if not client_email:
            return JSONResponse({"success": False, "message": "Client email is required"}, status_code=400)

        conn = get_db_connection()
        cursor = conn.cursor()
        if DATABASE_URL:
            cursor.execute("SELECT client_name, amount, due_date FROM invoices WHERE invoice_id = %s OR id::text = %s", (invoice_id, invoice_id))
        else:
            cursor.execute("SELECT client_name, amount, due_date FROM invoices WHERE invoice_id = ? OR id = ?", (invoice_id, invoice_id))
        invoice = cursor.fetchone()
        cursor.close()
        conn.close()

        if not invoice:
            return JSONResponse({"success": False, "message": "Invoice not found"}, status_code=404)

        client_name, raw_amount, due_date = invoice
        amount = float(raw_amount)

        resend.api_key = os.environ.get("RESEND_API_KEY")
        sender_email = os.environ.get("SENDER_EMAIL", "onboarding@resend.dev")

        if not resend.api_key:
            return JSONResponse({"success": False, "message": "RESEND_API_KEY is not configured on the server."}, status_code=500)

        html_content = f"""
        <p>Hi {client_name},</p>
        <p>This is a quick note to remind you about invoice <strong>{invoice_id}</strong> for <strong>${amount:.2f}</strong>, which was due on <strong>{due_date}</strong>.</p>
        <p>Please let me know when you might be able to process this payment.</p>
        <p>Thank you!</p>
        """

        params = {
            "from": sender_email,
            "to": [client_email],
            "subject": f"Payment Reminder: Invoice {invoice_id}",
            "html": html_content,
        }

        email_response = resend.Emails.send(params)
        return JSONResponse({"success": True, "message": "Email sent successfully!", "data": email_response})

    except Exception as e:
        print(f"Email Send Error: {str(e)}")
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)