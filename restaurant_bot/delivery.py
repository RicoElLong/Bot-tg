"""One outbound gateway: sandbox, rate limiting and conservative retries."""
import asyncio
from datetime import timedelta
import json
import time
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from .storage import now


class Gateway:
    def __init__(self, config, store, bot, sleep=asyncio.sleep):
        self.config, self.store, self.bot, self.sleep = config, store, bot, sleep
        self.lock = asyncio.Lock()
        self.last_global = 0.0
        self.last_chat = {}

    async def send(self, chat_id, text, *, key, photo=None, markup=None, permitted=None):
        payload = {"text": text, "photo_id": photo, "reply_markup": markup.to_dict() if markup else None}
        with self.store.connect() as conn:
            previous = conn.execute("SELECT status FROM messages WHERE delivery_key=?", (key,)).fetchone()
            if previous:
                return previous[0]
            conn.execute("INSERT INTO messages(delivery_key,chat_id,payload,status,created_at) VALUES(?,?,?,?,?)", (key, chat_id, json.dumps(payload, ensure_ascii=False), "prepared", now()))
        if self.config.sandbox:
            return self.finish(key, "sandbox" if permitted is None or permitted() else "skipped")
        if not self.config.real_sends or not self.config.polling:
            return self.finish(key, "failed", "sending_disabled")
        for attempt in range(4):
            retry_seconds = None
            async with self.lock:
                wait = max(0.0, self.last_global + .06 - time.monotonic(), self.last_chat.get(chat_id, 0) + 1.1 - time.monotonic())
                await self.sleep(wait)
                if permitted is not None and not permitted():
                    return self.finish(key, "skipped", "consent_or_cancel")
                self.finish(key, "sending")
                self.last_global = self.last_chat[chat_id] = time.monotonic()
                try:
                    if photo:
                        await self.bot.send_photo(chat_id=chat_id, photo=photo, caption=text, reply_markup=markup)
                    else:
                        await self.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
                    return self.finish(key, "sent")
                except RetryAfter as exc:
                    if attempt == 3:
                        return self.finish(key, "failed", "rate_limit")
                    value = exc.retry_after
                    retry_seconds = value.total_seconds() if isinstance(value, timedelta) else float(value)
                    self.finish(key, "prepared", "rate_limit")
                except Forbidden:
                    with self.store.connect() as conn:
                        conn.execute("UPDATE clients SET blocked=1 WHERE id=?", (chat_id,))
                    return self.finish(key, "failed", "forbidden")
                except BadRequest:
                    return self.finish(key, "failed", "bad_request")
                except (NetworkError, TelegramError):
                    return self.finish(key, "uncertain", "telegram_uncertain")
            # Do not hold the shared limiter during a long flood-wait: guest opt-outs
            # must remain responsive. Every retry rechecks consent after this wait.
            if retry_seconds is not None:
                if retry_seconds > (30 if permitted is not None else 3):
                    return self.finish(key, "failed", "rate_limit_long_wait")
                await self.sleep(max(1.0, min(retry_seconds, 86400.0)))
        return self.finish(key, "uncertain", "interrupted")

    def finish(self, key, status, error=None):
        with self.store.connect() as conn:
            conn.execute("UPDATE messages SET status=?,error=? WHERE delivery_key=?", (status, error, key))
        return status

    async def answer(self, query):
        if not self.config.sandbox and self.config.real_sends:
            try:
                await query.answer()
            except TelegramError:
                pass


async def run_recipient(campaigns, gateway):
    row = campaigns.next_recipient()
    if not row:
        return False
    cid, key, client = row["campaign_id"], row["recipient_key"], row["client_id"]
    if row["creator_id"] not in gateway.config.admins:
        campaigns.finish(cid, key, "skipped", "admin_revoked")
        return True
    if not campaigns.claim(cid, key):
        campaigns.finish(cid, key, "skipped", "consent_or_cancel")
        return True

    def permitted():
        with campaigns.store.connect() as conn:
            state = conn.execute("SELECT c.state,u.blocked FROM campaigns c LEFT JOIN clients u ON u.id=? WHERE c.id=?", (client, cid)).fetchone()
            return bool(state and state[0] in {"queued", "running"} and state[1] == 0 and campaigns.store.allowed(conn, client, "pd") and campaigns.store.allowed(conn, client, "marketing"))

    status = await gateway.send(client, row["text"], key=f"campaign:{cid}:{key}", photo=row["photo_id"], permitted=permitted)
    campaigns.finish(cid, key, status, None if status in {"sent", "sandbox"} else status)
    return True


async def campaign_loop(campaigns, gateway, stop):
    while not stop.is_set():
        if not await run_recipient(campaigns, gateway):
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except TimeoutError:
                pass
