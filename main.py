from contextlib import asynccontextmanager
import os
from database import get_db_connection, init_db
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


# Lifespan context manager runs database setup on app startup
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Payback AI", lifespan=lifespan)

# Mount static directory if it exists
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# Templates setup
templates = Jinja2Templates(directory="templates")


# --- ROUTES ---


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    conn = get_db_connection()
    debts = conn.execute("SELECT * FROM debts").fetchall()
    conn.close()

    # Fixed syntax for newer Starlette/FastAPI versions
    return templates.TemplateResponse(
        request=request, name="index.html", context={"debts": debts}
    )


@app.post("/add")
async def add_debt(
    person_name: str = Form(...),
    amount: float = Form(...),
    description: str = Form(""),
):
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO debts (person_name, amount, description) VALUES (?, ?, ?)",
        (person_name, amount, description),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)


@app.post("/delete/{debt_id}")
async def delete_debt(debt_id: int):
    conn = get_db_connection()
    conn.execute("DELETE FROM debts WHERE id = ?", (debt_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)