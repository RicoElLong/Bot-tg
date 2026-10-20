"""Author-bound drafts, atomic confirmation and a durable audience snapshot."""
import secrets
from .storage import Store, now


def audit(conn, admin_id: int, action: str, campaign_id: int | None = None):
    conn.execute("INSERT INTO audit(admin_id,action,campaign_id,occurred_at) VALUES(?,?,?,?)", (admin_id, action, campaign_id, now()))


class Campaigns:
    def __init__(self, store: Store):
        self.store = store

    def create(self, admin_id: int, text: str, photo_id: str | None = None) -> int:
        text = text.strip()
        if not text or len(text) > (1024 if photo_id else 4096):
            raise ValueError("Текст: 1–1024 символа с фото или 1–4096 без фото")
        with self.store.connect() as conn:
            cur = conn.execute("INSERT INTO campaigns(creator_id,text,photo_id,confirm_token,created_at) VALUES(?,?,?,?,?)", (admin_id, text, photo_id, secrets.token_urlsafe(12), now()))
            campaign_id = cur.lastrowid
            audit(conn, admin_id, "campaign_created", campaign_id)
            return campaign_id

    def get(self, campaign_id: int, admin_id: int):
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM campaigns WHERE id=? AND creator_id=?", (campaign_id, admin_id)).fetchone()
            return dict(row) if row else None

    def confirm(self, campaign_id: int, admin_id: int, token: str) -> int | None:
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute("UPDATE campaigns SET state='queued',confirmed_at=? WHERE id=? AND creator_id=? AND confirm_token=? AND state='draft'", (now(), campaign_id, admin_id, token)).rowcount
            if not changed:
                return None
            clients = conn.execute("SELECT id FROM clients WHERE blocked=0").fetchall()
            for row in clients:
                if self.store.allowed(conn, row[0], "pd") and self.store.allowed(conn, row[0], "marketing"):
                    conn.execute("INSERT INTO recipients(campaign_id,client_id,recipient_key) VALUES(?,?,?)", (campaign_id, row[0], secrets.token_hex(12)))
            count = conn.execute("SELECT count(*) FROM recipients WHERE campaign_id=?", (campaign_id,)).fetchone()[0]
            if not count:
                conn.execute("UPDATE campaigns SET state='done' WHERE id=?", (campaign_id,))
            audit(conn, admin_id, "campaign_confirmed", campaign_id)
            return count

    def cancel(self, campaign_id: int, admin_id: int):
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute("UPDATE campaigns SET state='cancelled' WHERE id=? AND creator_id=? AND state IN ('draft','queued','running')", (campaign_id, admin_id)).rowcount
            if changed:
                conn.execute("UPDATE recipients SET status='skipped',error='cancelled' WHERE campaign_id=? AND status='pending'", (campaign_id,))
                audit(conn, admin_id, "campaign_cancelled", campaign_id)
            return bool(changed)

    def next_recipient(self):
        with self.store.connect() as conn:
            row = conn.execute("SELECT r.*,c.text,c.photo_id,c.creator_id FROM recipients r JOIN campaigns c ON c.id=r.campaign_id WHERE r.status='pending' AND c.state IN ('queued','running') ORDER BY r.campaign_id LIMIT 1").fetchone()
            return dict(row) if row else None

    def claim(self, campaign_id: int, key: str):
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT r.client_id,c.state,u.blocked FROM recipients r JOIN campaigns c ON c.id=r.campaign_id LEFT JOIN clients u ON u.id=r.client_id WHERE r.campaign_id=? AND r.recipient_key=? AND r.status='pending'", (campaign_id, key)).fetchone()
            if not row:
                return False
            permitted = row[0] and not row[2] and row[1] in {"queued", "running"} and self.store.allowed(conn, row[0], "pd") and self.store.allowed(conn, row[0], "marketing")
            conn.execute("UPDATE recipients SET status=?,error=? WHERE campaign_id=? AND recipient_key=?", ("sending" if permitted else "skipped", None if permitted else "consent_or_cancel", campaign_id, key))
            if permitted:
                conn.execute("UPDATE campaigns SET state='running' WHERE id=?", (campaign_id,))
            return bool(permitted)

    def finish(self, campaign_id: int, key: str, status: str, error: str | None = None):
        with self.store.connect() as conn:
            conn.execute("UPDATE recipients SET status=?,error=? WHERE campaign_id=? AND recipient_key=?", (status, error, campaign_id, key))
            if not conn.execute("SELECT 1 FROM recipients WHERE campaign_id=? AND status IN ('pending','sending')", (campaign_id,)).fetchone():
                conn.execute("UPDATE campaigns SET state='done' WHERE id=? AND state IN ('queued','running')", (campaign_id,))

    def recover(self):
        with self.store.connect() as conn:
            conn.execute("UPDATE messages SET status='uncertain',error='interrupted' WHERE status IN ('sending','prepared')")
            conn.execute("UPDATE recipients SET status='uncertain',error='interrupted' WHERE status='sending'")
            conn.execute("UPDATE campaigns SET state='done' WHERE state IN ('queued','running') AND NOT EXISTS(SELECT 1 FROM recipients r WHERE r.campaign_id=campaigns.id AND r.status IN ('pending','sending'))")

    def history(self, admin_id: int):
        with self.store.connect() as conn:
            audit(conn, admin_id, "history_viewed")
            rows = [dict(r) for r in conn.execute("SELECT id,creator_id,state,created_at FROM campaigns ORDER BY id DESC LIMIT 10")]
            for row in rows:
                row["counts"] = dict(conn.execute("SELECT status,count(*) FROM recipients WHERE campaign_id=? GROUP BY status", (row["id"],)).fetchall())
            return rows
