import os

from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
TOKEN = os.environ["BOT_TOKEN"]

MENU = ReplyKeyboardMarkup(
    [
        ["Акции", "Меню"],
        ["Доставка", "Контакты"],
        ["Вкусная рулетка"],
    ],
    resize_keyboard=True,
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Добро пожаловать в ресторан «Дети лейтенанта Шмидта»!\n\n"
        "Выберите раздел:",
        reply_markup=MENU,
    )


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text
    if choice == "Вкусная рулетка":
        # Do not claim that a spin or a prize was issued until the secure
        # Mini App, prize inventory, and server-side draw are implemented.
        await update.message.reply_text(
            "Вкусная рулетка скоро появится! 🎁\n\n"
            "Первое вращение будет доступно после регистрации, "
            "а новые попытки — в случайный момент в течение недели "
            "только в часы работы ресторана. "
            "Мы сообщим о новой попытке в боте."
        )
    else:
        await update.message.reply_text(
            f"Раздел «{choice}» пока готовится.",
            reply_markup=MENU,
        )


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
    print("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
