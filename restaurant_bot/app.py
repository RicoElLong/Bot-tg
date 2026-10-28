"""Composition and explicitly enabled long polling. No webhook changes."""
import asyncio
from collections import deque
import logging
import os
import time
from dotenv import load_dotenv
from telegram import BotCommand
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from .admin import Admin
from .campaigns import Campaigns
from .config import Config
from .delivery import Gateway, campaign_loop
from .guest import Guest
from .instance import instance_lock
from .storage import Store
from .polling import PollingBot


log = logging.getLogger("restaurant_bot")


def build_application(cfg: Config, store: Store, builder=None):
    app = (builder or ApplicationBuilder()).bot(PollingBot(cfg.token)).concurrent_updates(False).build()
    gateway = Gateway(cfg, store, app.bot)
    campaigns = Campaigns(store)
    guest = Guest(cfg, store, gateway)
    admin = Admin(cfg, store, campaigns, guest)
    stop = asyncio.Event()

    def guarded(handler):
        async def wrapped(update, context):
            if not update.effective_chat or update.effective_chat.type != "private" or not update.effective_user:
                return
            # Bound in-memory per-chat rate window; never write unsolicited IDs to logs.
            window = context.chat_data.setdefault("rate_window", deque(maxlen=31))
            tick = time.monotonic()
            while window and window[0] < tick - 60:
                window.popleft()
            if len(window) >= 30:
                return
            window.append(tick)
            if not store.claim_update(update.update_id):
                return
            context.chat_data["reply_number"] = 0
            await handler(update, context)
            store.complete_update(update.update_id)
        return wrapped

    for name, fn in {"start":guest.start, "help":guest.help, "privacy":guest.privacy,
                     "profile":guest.profile, "subscribe":guest.offer_marketing,
                     "unsubscribe":guest.unsubscribe, "stop":guest.unsubscribe,
                     "delete_me":guest.delete_me, "admin":admin.panel, "cancel":admin.cancel_input}.items():
        app.add_handler(CommandHandler(name, guarded(fn)))
    app.add_handler(CallbackQueryHandler(guarded(admin.callback), pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(guarded(guest.callback)))
    async def dispatch_message(update, context):
        if context.user_data.get("reg_step"):
            await guest.registration_message(update, context)
        else:
            await admin.message(update, context)

    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND & (filters.TEXT | filters.PHOTO | filters.CONTACT), guarded(dispatch_message)))

    async def on_error(_update, context):
        log.error("Ошибка обработчика: %s; содержимое события скрыто", type(context.error).__name__)

    async def worker():
        try:
            await campaign_loop(campaigns, gateway, stop)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Очередь остановлена: %s", type(exc).__name__)
            app.stop_running()

    async def post_init(application):
        await application.bot.require_no_webhook()
        campaigns.recover()
        application.bot_data["campaign_task"] = asyncio.create_task(worker())
        if not cfg.sandbox and cfg.real_sends:
            await application.bot.set_my_commands([
                BotCommand("start", "Главное меню"), BotCommand("help", "Помощь"),
                BotCommand("profile", "Мой профиль"), BotCommand("privacy", "Согласия"),
                BotCommand("unsubscribe", "Отказаться от рекламы")])

    async def post_stop(application):
        stop.set()
        task = application.bot_data.get("campaign_task")
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app.add_error_handler(on_error)
    app.post_init, app.post_stop = post_init, post_stop
    app.bot_data.update(guest=guest, admin=admin, gateway=gateway, campaigns=campaigns)
    return app


def main():
    # USER_ACTION: включать polling/реальные отправки только после согласования и замены токена.
    load_dotenv(override=False)
    if os.name != "nt":
        os.umask(0o077)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").disabled = True
    logging.getLogger("httpcore").disabled = True
    logging.getLogger("telegram").setLevel(logging.CRITICAL)
    try:
        cfg = Config.load()
        cfg.validate_launch()
        with instance_lock(cfg.database):
            store = Store(cfg.database)
            store.bind_mode("sandbox" if cfg.sandbox else "live")
            store.pin_documents(cfg)
            app = build_application(cfg, store)
            print("Режим: " + ("SANDBOX (исходящие сообщения только в SQLite)" if cfg.sandbox else "РЕАЛЬНЫЕ ОТПРАВКИ"))
            # PTB run_polling performs network initialization. Never called by tests/imports.
            # PollingBot refuses an existing webhook; PTB's bootstrap cannot delete it.
            app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False, bootstrap_retries=0)
    except Exception as exc:
        if isinstance(exc, ValueError):
            raise SystemExit(str(exc)) from None
        raise SystemExit("Запуск остановлен: " + type(exc).__name__ + ". Токен и данные скрыты.") from None
