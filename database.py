import sqlite3
import os
from dotenv import load_dotenv

load_dotenv()

DB_NAME = os.getenv("DATABASE_URL", "payback.db")

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Create users table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)
    
    # Create logs table with user association
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            client_name TEXT NOT NULL,
            amount INTEGER NOT NULL,
            notice_type TEXT NOT NULL,
            due_date TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    
    # Migrations for existing tables if needed
    try:
        cursor.execute("ALTER TABLE logs ADD COLUMN due_date TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute("ALTER TABLE logs ADD COLUMN user_id INTEGER")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()