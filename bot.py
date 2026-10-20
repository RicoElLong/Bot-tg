import os

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

TOKEN = os.environ["BOT_TOKEN"]


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "Добро пожаловать в ресторан "
        "«Дети лейтенанта Шмидта»!\n\n"
        "Здесь вы сможете узнавать "
        "о наших акциях и новостях."
    )


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))

    print("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
