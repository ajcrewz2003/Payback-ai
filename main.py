from contextlib import asynccontextmanager
import os
from database import get_db_connection, init_db
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure database tables exist on boot
    init_db()
    yield


app = FastAPI(title="Payback AI", lifespan=lifespan)

# Mount static folder only if it exists
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# Mount templates folder if it exists
templates = (
    Jinja2Templates(directory="templates")
    if os.path.exists("templates")
    else None
)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    conn = get_db_connection()
    debts = conn.execute("SELECT * FROM debts").fetchall()
    conn.close()

    if templates:
        return templates.TemplateResponse(
            "index.html", {"request": request, "debts": debts}
        )

    return HTMLResponse("<h1>Payback AI is Live!</h1>")


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