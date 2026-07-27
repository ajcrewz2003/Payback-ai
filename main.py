from contextlib import asynccontextmanager
import json
import os
import sqlite3
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google import genai
from google.genai import types

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


# --- GEMINI AI PARSER ENDPOINT ---


@app.post("/api/ai-add")
async def ai_ai_add_invoice(request: Request):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return JSONResponse(
            {
                "status": "error",
                "message": "GEMINI_API_KEY environment variable is missing on Render.",
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

    client = genai.Client(api_key=api_key)

    system_instruction = (
        "Extract invoice details from user text. Return structured JSON with keys:\n"
        "- invoice_id: string (generate short unique code like Inv_101 if missing)\n"
        "- client_name: string (person or company name)\n"
        "- amount: number (float value)\n"
        "- due_date: string (YYYY-MM-DD or readable date string)\n"
        "- status: string (must be exactly 'FRIENDLY', 'URGENT', or 'PAID')"
    )

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
            ),
        )

        extracted = json.loads(response.text)

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
                "message": "AI generated an ID that already exists. Try again.",
            },
            status_code=400,
        )
    except Exception as e:
        return JSONResponse(
            {"status": "error", "message": f"AI Parsing failed: {str(e)}"},
            status_code=500,
        )


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
    if cursor.rowcount == 0:
        cursor.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))

    conn.commit()
    conn.close()

    return JSONResponse(
        {"status": "success", "message": "Invoice deleted successfully"}
    )