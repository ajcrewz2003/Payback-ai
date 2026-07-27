import os
from fastapi import FastAPI, Request, HTTPException, Depends, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
import hashlib

from database import get_db_connection, init_db

load_dotenv()

app = FastAPI(title="Payback AI")

init_db()

app.mount("/static", StaticFiles(directory="STATIC"), name="static")
templates = Jinja2Templates(directory="TEMPLATES")


class NoticeCreate(BaseModel):
    id: str
    client_name: str
    amount: int
    notice_type: str
    due_date: Optional[str] = None


class UserAuth(BaseModel):
    username: str
    password: str


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def get_current_user(request: Request):
    user_id = request.cookies.get("user_id")
    username = request.cookies.get("username")
    if not user_id or not username:
        return None
    return {"id": int(user_id), "username": username}


@app.get("/")
def root(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/dashboard", status_code=303)
    return RedirectResponse(url="/login", status_code=303)


@app.get("/login")
def render_login(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
async def handle_login(request: Request, auth: UserAuth):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ? AND password = ?", (auth.username, hash_password(auth.password)))
    user = cursor.fetchone()
    conn.close()

    if not user:
        raise HTTPException(status_code=400, detail="Invalid username or password")

    response = Response(content='{"status": "success"}', media_type="application/json")
    response.set_cookie(key="user_id", value=str(user["id"]), httponly=True)
    response.set_cookie(key="username", value=user["username"], httponly=True)
    return response


@app.post("/register")
async def handle_register(auth: UserAuth):
    if not auth.username or not auth.password:
        raise HTTPException(status_code=400, detail="Username and password required")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (username, password) VALUES (?, ?)", (auth.username, hash_password(auth.password)))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Username already exists")
    conn.close()
    return {"status": "registered"}


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("user_id")
    response.delete_cookie("username")
    return response


@app.get("/dashboard")
def render_dashboard(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse("index.html", {"request": request, "username": user["username"]})


@app.get("/notices")
def fetch_notices(request: Request, notice_type: Optional[str] = None):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    conn = get_db_connection()
    cursor = conn.cursor()
    if notice_type:
        cursor.execute("SELECT id, client_name, amount, notice_type, due_date FROM logs WHERE user_id = ? AND notice_type = ?", (user["id"], notice_type))
    else:
        cursor.execute("SELECT id, client_name, amount, notice_type, due_date FROM logs WHERE user_id = ?", (user["id"],))
    rows = cursor.fetchall()
    conn.close()

    data = [dict(row) for row in rows]
    return {"data": data}


@app.post("/notices")
def create_notice(request: Request, notice: NoticeCreate):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM logs WHERE id = ? AND user_id = ?", (notice.id, user["id"]))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Notice ID already exists")

    cursor.execute(
        "INSERT INTO logs (id, user_id, client_name, amount, notice_type, due_date) VALUES (?, ?, ?, ?, ?, ?)",
        (notice.id, user["id"], notice.client_name, notice.amount, notice.notice_type, notice.due_date)
    )
    conn.commit()
    conn.close()
    return {"status": "created", "id": notice.id}


@app.put("/notices/{notice_id}")
def update_notice(request: Request, notice_id: str, notice: NoticeCreate):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE logs SET client_name = ?, amount = ?, notice_type = ?, due_date = ? WHERE id = ? AND user_id = ?",
        (notice.client_name, notice.amount, notice.notice_type, notice.due_date, notice_id, user["id"])
    )
    if cursor.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Notice not found")
    conn.commit()
    conn.close()
    return {"status": "updated", "id": notice_id}


@app.delete("/notices/{notice_id}")
def delete_notice(request: Request, notice_id: str):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM logs WHERE id = ? AND user_id = ?", (notice_id, user["id"]))
    if cursor.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Notice not found")
    conn.commit()
    conn.close()
    return {"status": "deleted", "id": notice_id}


@app.get("/export-csv")
def export_csv(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, client_name, amount, notice_type, due_date FROM logs WHERE user_id = ?", (user["id"],))
    rows = cursor.fetchall()
    conn.close()

    csv_lines = ["Invoice ID,Client Name,Amount,Status,Due Date"]
    for r in rows:
        csv_lines.append(f"{r['id']},{r['client_name']},{r['amount']},{r['notice_type']},{r['due_date'] or 'N/A'}")

    csv_content = "\n".join(csv_lines)
    return Response(content=csv_content, media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=payback_logs_{user['username']}.csv"})