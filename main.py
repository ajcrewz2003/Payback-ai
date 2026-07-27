from contextlib import asynccontextmanager
import os
import sqlite3
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

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

    # Calculate dashboard header statistics (exclude PAID from outstanding total)
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