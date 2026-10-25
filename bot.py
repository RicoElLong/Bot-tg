import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = Path(os.getenv("BOT_DB_PATH", "bot_users.sqlite3"))
PRIVACY_URL = os.getenv("PRIVACY_POLICY_URL", "").strip()
CONSENT_TEXT = os.getenv("PERSONAL_DATA_CONSENT_TEXT", "").strip()

MENU = ReplyKeyboardMarkup(
    [["Акции", "Меню"], ["Доставка", "Контакты"], ["Вкусная рулетка"]],
    resize_keyboard=True,
)
CONSENT_KEYS = ReplyKeyboardMarkup(
    [["Согласен на обработку данных"], ["Отказываюсь"]],
    resize_keyboard=True,
)
PHONE_KEYS = ReplyKeyboardMarkup(
    [[KeyboardButton("Отправить мой номер", request_contact=True)], ["Отмена"]],
    resize_keyboard=True,
)
BIRTHDAY_KEYS = ReplyKeyboardMarkup(
    [["Пропустить"], ["Отмена"]],
    resize_keyboard=True,
)


def db_open():
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA busy_timeout=5000")
    db.execute(
        "CREATE TABLE IF NOT EXISTS guests ("
        "telegram_id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL,"
        "birthday TEXT, data_consent_at TEXT NOT NULL,"
        "data_consent_text TEXT NOT NULL, marketing_consent INTEGER NOT NULL DEFAULT 0)"
    )
    db.commit()
    return db


def registered(user_id):
    with db_open() as db:
        return db.execute(
            "SELECT 1 FROM guests WHERE telegram_id=?", (user_id,)
        ).fetchone() is not None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if registered(update.effective_user.id):
        await update.message.reply_text("С возвращением! Выберите раздел:", reply_markup=MENU)
        return
    if not PRIVACY_URL.startswith("https://") or not CONSENT_TEXT:
        await update.message.reply_text(
            "Регистрация временно недоступна: администратор должен указать "
            "ссылку на политику и текст согласия."
        )
        return
    context.user_data["step"] = "consent"
    await update.message.reply_text(
        "Ознакомьтесь с политикой обработки персональных данных:\n"
        + PRIVACY_URL + "\n\n" + CONSENT_TEXT,
        reply_markup=CONSENT_KEYS,
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Регистрация отменена. Для повторной попытки отправьте /start."
    )


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    value = (msg.text or "").strip()
    step = context.user_data.get("step")
    if step and value in ("Отмена", "Отказываюсь"):
        await cancel(update, context)
        return
    if step == "consent":
        if value != "Согласен на обработку данных":
            await msg.reply_text("Выберите согласие или отказ.", reply_markup=CONSENT_KEYS)
            return
        context.user_data.update(
            step="name",
            data_consent_at=datetime.now(timezone.utc).isoformat(),
        )
        await msg.reply_text("Введите ваше имя.")
        return
    if step == "name":
        if not 2 <= len(value) <= 80:
            await msg.reply_text("Имя должно содержать от 2 до 80 символов.")
            return
        context.user_data.update(step="phone", name=value)
        await msg.reply_text(
            "Отправьте свой номер кнопкой или введите его вручную (10–15 цифр).",
            reply_markup=PHONE_KEYS,
        )
        return
    if step == "phone":
        if msg.contact:
            if msg.contact.user_id != update.effective_user.id:
                await msg.reply_text("Отправьте собственный контакт.")
                return
            phone = msg.contact.phone_number
        else:
            phone = value
        phone = re.sub(r"[\s()\-]", "", phone)
        if not re.fullmatch(r"\+?\d{10,15}", phone):
            await msg.reply_text("Неверный формат номера. Введите 10–15 цифр.")
            return
        context.user_data.update(step="birthday", phone=phone)
        await msg.reply_text(
            "Укажите дату рождения в формате ДД.ММ.ГГГГ для персонального бонуса "
            "или нажмите «Пропустить».",
            reply_markup=BIRTHDAY_KEYS,
        )
        return
    if step == "birthday":
        birthday = None
        if value != "Пропустить":
            try:
                birthday = datetime.strptime(value, "%d.%m.%Y").strftime("%d.%m.%Y")
            except ValueError:
                await msg.reply_text("Введите дату ДД.ММ.ГГГГ или нажмите «Пропустить».")
                return
        with db_open() as db:
            db.execute(
                "INSERT OR IGNORE INTO guests "
                "(telegram_id, name, phone, birthday, data_consent_at, "
                "data_consent_text, marketing_consent) VALUES (?, ?, ?, ?, ?, ?, 0)",
                (
                    update.effective_user.id,
                    context.user_data["name"],
                    context.user_data["phone"],
                    birthday,
                    context.user_data["data_consent_at"],
                    CONSENT_TEXT,
                ),
            )
            db.commit()
        context.user_data.clear()
        await msg.reply_text("Регистрация завершена! Выберите раздел:", reply_markup=MENU)
        return
    if not registered(update.effective_user.id):
        await start(update, context)
        return
    if value == "Вкусная рулетка":
        await msg.reply_text(
            "Вкусная рулетка пока в разработке. Первая попытка будет "
            "доступна после запуска игры.",
            reply_markup=MENU,
        )
    elif value in ("Акции", "Меню", "Доставка", "Контакты"):
        await msg.reply_text(f"Раздел «{value}» пока готовится.", reply_markup=MENU)
    else:
        await msg.reply_text("Выберите раздел из меню.", reply_markup=MENU)


def main():
    with db_open():
        pass
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler((filters.TEXT | filters.CONTACT) & ~filters.COMMAND, handle))
    print("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
