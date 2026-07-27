from contextlib import asynccontextmanager
import os
from database import init_db
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


# Lifespan event handler ensures init_db runs on startup on Render
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs when app starts up
    init_db()
    yield


app = FastAPI(title="Payback AI", lifespan=lifespan)

# Mount static files (CSS, JS, images)
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# Templates setup
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})