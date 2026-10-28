"""SQLite connections, schema, clients and versioned consent history."""
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES (1);
CREATE TABLE IF NOT EXISTS runtime_mode(mode TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS processed_updates(id INTEGER PRIMARY KEY, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(kind TEXT NOT NULL, version TEXT NOT NULL, document TEXT NOT NULL, PRIMARY KEY(kind,version));
CREATE TABLE IF NOT EXISTS clients(
 id INTEGER PRIMARY KEY, registered_at TEXT NOT NULL,
 first_source TEXT NOT NULL, last_source TEXT NOT NULL,
 blocked INTEGER NOT NULL DEFAULT 0 CHECK(blocked IN (0,1))
);
CREATE TABLE IF NOT EXISTS guest_profiles(
 client_id INTEGER PRIMARY KEY REFERENCES clients(id) ON DELETE CASCADE,
 phone TEXT NOT NULL, birthday TEXT, completed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consents(
 id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('pd','marketing')), granted INTEGER NOT NULL,
 version TEXT NOT NULL, document TEXT NOT NULL, occurred_at TEXT NOT NULL,
 source TEXT NOT NULL, event_key TEXT NOT NULL,
 UNIQUE(client_id, kind, event_key)
);
CREATE TABLE IF NOT EXISTS interactions(
 id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
 event TEXT NOT NULL, source TEXT NOT NULL, occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaigns(
 id INTEGER PRIMARY KEY, creator_id INTEGER NOT NULL, text TEXT NOT NULL,
 photo_id TEXT, state TEXT NOT NULL DEFAULT 'draft', confirm_token TEXT NOT NULL,
 created_at TEXT NOT NULL, confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS recipients(
 campaign_id INTEGER NOT NULL REFERENCES campaigns(id), client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
 recipient_key TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', error TEXT,
 PRIMARY KEY(campaign_id, recipient_key)
);
CREATE TABLE IF NOT EXISTS messages(
 id INTEGER PRIMARY KEY, delivery_key TEXT UNIQUE NOT NULL, chat_id INTEGER,
 payload TEXT NOT NULL, status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit(
 id INTEGER PRIMARY KEY, admin_id INTEGER NOT NULL, action TEXT NOT NULL,
 campaign_id INTEGER, occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS consent_latest ON consents(client_id,kind,id DESC);
CREATE INDEX IF NOT EXISTS recipient_pending ON recipients(status,campaign_id);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables and "schema_version" not in tables:
                raise ValueError("Неизвестная существующая база: выберите новый DATABASE_PATH, старый файл сохранён")
            if "schema_version" in tables and {r[0] for r in conn.execute("SELECT version FROM schema_version")} != {1}:
                raise ValueError("Версия базы не поддерживается; требуется отдельная миграция")
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def bind_mode(self, mode: str):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT mode FROM runtime_mode").fetchone()
            if previous and previous[0] != mode:
                raise ValueError("Sandbox и реальные отправки требуют разных DATABASE_PATH")
            conn.execute("INSERT OR IGNORE INTO runtime_mode VALUES(?)", (mode,))

    def pin_documents(self, cfg):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for kind, version, document in (("pd", cfg.pd_version, cfg.pd_text), ("marketing", cfg.marketing_version, cfg.marketing_text)):
                existing = conn.execute("SELECT document FROM documents WHERE kind=? AND version=?", (kind, version)).fetchone()
                if existing and existing[0] != document:
                    raise ValueError("Текст согласия изменён: задайте новую версию документа")
                conn.execute("INSERT OR IGNORE INTO documents VALUES(?,?,?)", (kind, version, document))

    def claim_update(self, update_id: int) -> bool:
        with self.connect() as conn:
            return bool(conn.execute("INSERT OR IGNORE INTO processed_updates VALUES(?,'processing')", (update_id,)).rowcount)

    def complete_update(self, update_id: int):
        with self.connect() as conn:
            conn.execute("UPDATE processed_updates SET status='done' WHERE id=?", (update_id,))

    @staticmethod
    def allowed(conn, client_id: int, kind: str) -> bool:
        row = conn.execute("SELECT granted FROM consents WHERE client_id=? AND kind=? ORDER BY id DESC LIMIT 1", (client_id, kind)).fetchone()
        return bool(row and row[0])

    def registered(self, client_id: int) -> bool:
        with self.connect() as conn:
            return self.allowed(conn, client_id, "pd")

    def registration_complete(self, client_id: int) -> bool:
        with self.connect() as conn:
            return self.allowed(conn, client_id, "pd") and conn.execute(
                "SELECT 1 FROM guest_profiles WHERE client_id=?", (client_id,)
            ).fetchone() is not None

    def complete_registration(self, client_id: int, source: str, version: str, document: str,
                              event_key: str, phone: str, birthday: str | None):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO clients(id,registered_at,first_source,last_source) VALUES(?,?,?,?)",
                         (client_id, now(), source, source))
            conn.execute("UPDATE clients SET blocked=0,last_source=? WHERE id=?", (source, client_id))
            if not self.allowed(conn, client_id, "pd"):
                conn.execute("INSERT OR IGNORE INTO consents(client_id,kind,granted,version,document,occurred_at,source,event_key) VALUES(?,'pd',1,?,?,?,?,?)",
                             (client_id, version, document, now(), source, event_key))
            conn.execute("INSERT INTO guest_profiles(client_id,phone,birthday,completed_at) VALUES(?,?,?,?) "
                         "ON CONFLICT(client_id) DO UPDATE SET phone=excluded.phone,birthday=excluded.birthday,completed_at=excluded.completed_at",
                         (client_id, phone, birthday, now()))

    def subscribed(self, client_id: int) -> bool:
        with self.connect() as conn:
            return self.allowed(conn, client_id, "pd") and self.allowed(conn, client_id, "marketing")

    def register(self, client_id: int, source: str, version: str, document: str, event_key: str):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO clients(id,registered_at,first_source,last_source) VALUES(?,?,?,?)", (client_id, now(), source, source))
            conn.execute("UPDATE clients SET blocked=0,last_source=? WHERE id=?", (source, client_id))
            if not self.allowed(conn, client_id, "pd"):
                conn.execute("INSERT OR IGNORE INTO consents(client_id,kind,granted,version,document,occurred_at,source,event_key) VALUES(?,'pd',1,?,?,?,?,?)", (client_id, version, document, now(), source, event_key))

    def consent(self, client_id: int, granted: bool, version: str, document: str, source: str, event_key: str):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self.allowed(conn, client_id, "pd"):
                return False
            conn.execute("INSERT OR IGNORE INTO consents(client_id,kind,granted,version,document,occurred_at,source,event_key) VALUES(?,'marketing',?,?,?,?,?,?)", (client_id, int(granted), version, document, now(), source, event_key))
            return True

    def visit(self, client_id: int, source: str):
        with self.connect() as conn:
            if self.allowed(conn, client_id, "pd"):
                conn.execute("UPDATE clients SET last_source=?,blocked=0 WHERE id=?", (source, client_id))
                conn.execute("INSERT INTO interactions(client_id,event,source,occurred_at) VALUES(?,'start',?,?)", (client_id, source, now()))

    def delete(self, client_id: int):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM messages WHERE chat_id=?", (client_id,))
            conn.execute("DELETE FROM clients WHERE id=?", (client_id,))
            conn.execute("UPDATE recipients SET status='skipped',error='deleted' WHERE client_id IS NULL AND status='pending'")

    def stats(self):
        with self.connect() as conn:
            total = conn.execute("SELECT count(*) FROM clients").fetchone()[0]
            subscribers = sum(self.allowed(conn, r[0], "pd") and self.allowed(conn, r[0], "marketing") and not r[1] for r in conn.execute("SELECT id,blocked FROM clients"))
            counts = dict(conn.execute("SELECT status,count(*) FROM recipients GROUP BY status").fetchall())
            sandbox = conn.execute("SELECT count(*) FROM messages WHERE status='sandbox'").fetchone()[0]
            return total, subscribers, counts, sandbox
