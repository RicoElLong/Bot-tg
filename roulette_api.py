"""Telegram Mini App roulette: demo-only spin, no redeemable prizes."""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qsl

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = Path(os.getenv("BOT_DB_PATH", "bot_users.sqlite3"))
BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(docs_url=None, redoc_url=None)
DEMO_PRIZES = ["Напиток", "Десерт", "Хачапури", "Скидка 10%", "Чай", "Сюрприз"]


class SpinRequest(BaseModel):
    init_data: str


def telegram_user(init_data: str) -> int:
    if not init_data or len(init_data) > 8192:
        raise HTTPException(401, "Telegram authorization required")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    check = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected):
        raise HTTPException(401, "Invalid Telegram signature")
    try:
        auth_date = int(pairs["auth_date"])
        user_id = int(json.loads(pairs["user"])["id"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(401, "Invalid Telegram data")
    if abs(time.time() - auth_date) > 3600 or user_id <= 0:
        raise HTTPException(401, "Expired Telegram session")
    return user_id


def connection():
    db = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("""CREATE TABLE IF NOT EXISTS roulette_spins (
        telegram_id INTEGER PRIMARY KEY,
        prize_index INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    )""")
    return db


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "roulette.html")


@app.post("/api/spin")
def spin(request: SpinRequest):
    user_id = telegram_user(request.init_data)
    with connection() as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                guest = db.execute(
                    "SELECT 1 FROM guests WHERE telegram_id=?", (user_id,)
                ).fetchone()
            except sqlite3.OperationalError:
                raise HTTPException(503, "Registration database unavailable")
            if not guest:
                raise HTTPException(403, "Register in the Telegram bot first")
            existing = db.execute(
                "SELECT prize_index FROM roulette_spins WHERE telegram_id=?", (user_id,)
            ).fetchone()
            if existing:
                db.execute("COMMIT")
                return {"status": "already_spun", "index": existing[0],
                        "prize": DEMO_PRIZES[existing[0]], "demo": True}
            index = secrets.randbelow(len(DEMO_PRIZES))
            db.execute(
                "INSERT INTO roulette_spins (telegram_id, prize_index, created_at) VALUES (?, ?, ?)",
                (user_id, index, int(time.time())),
            )
            db.execute("COMMIT")
            return {"status": "spun", "index": index,
                    "prize": DEMO_PRIZES[index], "demo": True}
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
