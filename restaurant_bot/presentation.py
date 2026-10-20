"""Guest-facing HTML typography. Configuration is always treated as plain text."""
from html import escape


WELCOME_TITLE = "Дети лейтенанта Шмидта"
WELCOME_TEXT = (
    "Добро пожаловать на борт.\n\n"
    "Волжское гостеприимство начинается со знакомства. "
    "Здесь удобно выбрать блюда, перейти к доставке и узнать о предложениях ресторана."
)


def brand(config):
    return f"<b>{escape(config.welcome_title)}</b>\n<i>На берегу Волги · Самара</i>"


def welcome(config):
    return f"{brand(config)}\n\n{escape(config.welcome_text)}"


def card(title, text):
    return f"<b>{escape(title)}</b>\n\n{escape(text)}"


def consent_document(title, intro, document):
    # Preserve the approved document verbatim; only the surrounding copy changes.
    return f"{card(title, intro)}\n\n{escape(document)}"
