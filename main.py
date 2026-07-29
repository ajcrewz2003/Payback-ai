from contextlib import asynccontextmanager
import csv
import io
import json
import os
import sqlite3
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openai import OpenAI
from datetime import datetime

DB_FILE = "payback.db"


def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
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
    invoices = conn.execute(
        "SELECT * FROM invoices ORDER BY id DESC"
    ).fetchall()
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
        """,
            (inv_id, client, amount, due_date, status),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return JSONResponse(
            {"status": "error", "message": "Invoice ID already exists"},
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
                "message": (
                    "OPENAI_API_KEY environment variable is missing on Render."
                ),
            },
            status_code=500,
        )

    data = await request.json()
    user_prompt = data.get("prompt", "")

    if not user_prompt:
        return JSONResponse(
            {"status": "error", "message": "Prompt cannot be empty"},
            status_code=400,
        )

    client = OpenAI(api_key=api_key)

    # Get the current live date and format it nicely
    current_date_str = datetime.now().strftime("%Y-%m-%d (%A)")

    system_instruction = (
    "Extract invoice details from user text. Return structured JSON with "
    "keys:\n- invoice_id: string (generate short unique code like Inv_101)\n"
    "- if missing\n- client_name: string (person or company name)\n-"
    "amount: number (float value)\n- due_date: string (YYYY-MM-DD or "
    "readable date string)\n- status: string (must be exactly 'FRIENDLY', "
    "'URGENT', or 'PAID')\n\n"
    f"CRITICAL CONTEXT: The current active calendar date is {current_date_str}. "
    "Always calculate any relative dates (like 'next week', 'tomorrow', or 'in 5 days') "
    f"relative to this current date ({current_date_str})."
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
        """,
            (inv_id, client_name, amount, due_date, status),
        )
        conn.commit()
        conn.close()

        return JSONResponse(
            {"status": "success", "message": "AI invoice created successfully!"}
        )

    except sqlite3.IntegrityError:
        return JSONResponse(
            {
                "status": "error",
                "message": (
                    "AI generated an ID that already exists. Try again."
                ),
            },
            status_code=400,
        )
    except Exception as e:
        return JSONResponse(
            {"status": "error", "message": f"AI Parsing failed: {str(e)}"},
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
        writer.writerow(
            [
                inv["invoice_id"],
                inv["client_name"],
                inv["amount"],
                inv["due_date"],
                inv["status"],
            ]
        )

    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=invoices_export.csv"
        },
    )


@app.get("/api/reminder-email/{invoice_id}")
async def get_reminder_email(invoice_id: str):
    conn = get_db_connection()
    inv = conn.execute(
        "SELECT * FROM invoices WHERE invoice_id = ? OR id = ?",
        (invoice_id, invoice_id),
    ).fetchone()
    conn.close()

    if not inv:
        return JSONResponse(
            {"status": "error", "message": "Invoice not found"}, status_code=404
        )

    subject = f"Friendly Reminder: Invoice {inv['invoice_id']} for ${inv['amount']}"
    body = (
        f"Hi {inv['client_name']},\n\n"
        f"I hope you're doing well! This is a quick note to remind you about invoice "
        f"{inv['invoice_id']} for ${inv['amount']:.2f}, which was due on {inv['due_date']}.\n\n"
        f"Please let me know when you might be able to process this payment. "
        f"Thank you so much!\n\nBest regards,\nYour Name"
    )

    return {"subject": subject, "body": body}


@app.post("/update-status/{invoice_id}")
async def update_status(invoice_id: str, request: Request):
    data = await request.json()
    new_status = data.get("status", "FRIENDLY")

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE invoices SET status = ? WHERE invoice_id = ?",
        (new_status, invoice_id),
    )
    if cursor.rowcount == 0:
        cursor.execute(
            "UPDATE invoices SET status = ? WHERE id = ?",
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

    cursor.execute("DELETE FROM invoices WHERE invoice_id = ?", (invoice_id,))
    if cursor.rowcount == "0" or cursor.rowcount == 0:
        cursor.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))

    conn.commit()
    conn.close()

    return JSONResponse(
        {"status": "success", "message": "Invoice deleted successfully"}
    )
from fastapi.responses import RedirectResponse

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login_action(request: Request):
    form = await request.form()
    username = form.get("username")
    password = form.get("password")
    
    # Add your validation logic here (e.g., check against env variables or database)
    if username == "admin" and password == "admin": # Change to your credentials
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(key="session_user", value=username)
        return response
    
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid username or password"})

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key="session_user")
    return response