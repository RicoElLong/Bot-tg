"""Generate an attribution link; the exact link can be encoded in any QR code."""
import argparse
from dotenv import load_dotenv
from .config import Config
from .guest import source_tag


def deep_link(username, source):
    if not username or source_tag(source) != source:
        raise ValueError("Нужны настоящий BOT_USERNAME и метка из 1–64 латинских букв, цифр, _ или -")
    return f"https://t.me/{username}?start={source}"


def main():
    load_dotenv(override=False)
    parser = argparse.ArgumentParser(description="Ссылка для рекламы или QR, без Telegram API")
    parser.add_argument("source", help="Например qr_table_01")
    args = parser.parse_args()
    try:
        print(deep_link(Config.load().username, args.source))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
