"""Explicitly enabled owner-only discovery; no messages or webhook mutations."""
import asyncio
import os
from dotenv import load_dotenv
from .config import Config, flag
from .polling import PollingBot


async def discover(cfg):
    async with PollingBot(cfg.token) as bot:
        await bot.require_no_webhook()
        updates = await bot.get_updates(timeout=0, allowed_updates=["message", "callback_query"])
        ids = sorted({u.effective_user.id for u in updates if u.effective_user and u.effective_chat and u.effective_chat.type == "private"})
        if not ids:
            print("Нет событий. Отправьте своему боту /start и повторите. Ответ бота не требуется.")
        else:
            print("Telegram ID из личных чатов (выберите СВОЙ ID; не добавляйте неизвестных):")
            for uid in ids:
                print(uid)


def main():
    load_dotenv(override=False)
    if not flag(os.environ, "ENABLE_TELEGRAM_POLLING"):
        raise SystemExit("Сеть выключена: сначала отдельно разрешите ENABLE_TELEGRAM_POLLING=true")
    cfg = Config.load()
    if not cfg.token:
        raise SystemExit("BOT_TOKEN не настроен")
    try:
        asyncio.run(discover(cfg))
    except Exception as exc:
        raise SystemExit("Проверка остановлена: " + type(exc).__name__ + ". Токен и события скрыты.") from None


if __name__ == "__main__":
    main()
