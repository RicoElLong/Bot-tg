from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlsplit
from .presentation import WELCOME_TITLE, WELCOME_TEXT


def flag(env: dict, name: str, default: str = "false") -> bool:
    value = env.get(name, default).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name}: используйте true или false")
    return value == "true"


@dataclass(frozen=True)
class Config:
    token: str
    admins: frozenset[int]
    database: Path
    backups: Path
    sandbox: bool
    polling: bool
    real_sends: bool
    notices_approved: bool
    pd_version: str
    pd_text: str
    marketing_version: str
    marketing_text: str
    menu_url: str
    delivery_url: str
    actions_url: str
    contacts: str
    username: str
    welcome_title: str = WELCOME_TITLE
    welcome_text: str = WELCOME_TEXT

    @classmethod
    def load(cls, env: dict | None = None):
        e = os.environ if env is None else env
        try:
            admins = frozenset(int(x.strip()) for x in e.get("ADMIN_IDS", "").split(",") if x.strip())
        except ValueError:
            raise ValueError("ADMIN_IDS: нужны числовые Telegram ID через запятую") from None
        if any(x <= 0 for x in admins):
            raise ValueError("ADMIN_IDS должны быть положительными")
        urls = []
        for name in ("MENU_URL", "DELIVERY_URL", "ACTIONS_URL"):
            value = e.get(name, "").strip()
            parsed = urlsplit(value)
            if value and (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password):
                raise ValueError(f"{name}: требуется HTTPS без логина и пароля")
            urls.append(value)
        username = e.get("BOT_USERNAME", "").strip().lstrip("@")
        if username and (not username.isascii() or not username.replace("_", "").isalnum()):
            raise ValueError("BOT_USERNAME: некорректный username")
        welcome_title = e.get("WELCOME_TITLE", WELCOME_TITLE).strip()
        welcome_text = e.get("WELCOME_TEXT", WELCOME_TEXT).strip()
        if not 1 <= len(welcome_title) <= 100 or not 1 <= len(welcome_text) <= 600:
            raise ValueError("WELCOME_TITLE: 1–100 символов; WELCOME_TEXT: 1–600 символов")
        return cls(
            token=e.get("BOT_TOKEN", ""), admins=admins,
            database=Path(e.get("DATABASE_PATH", "data/restaurant.sqlite3")),
            backups=Path(e.get("BACKUP_DIR", "backups")),
            sandbox=flag(e, "SANDBOX", "true"), polling=flag(e, "ENABLE_TELEGRAM_POLLING"),
            real_sends=flag(e, "ALLOW_REAL_SENDS"), notices_approved=flag(e, "NOTICES_APPROVED"),
            pd_version=e.get("PD_VERSION", "draft-pd-1"),
            pd_text=e.get("PD_TEXT", "ТЕСТОВЫЙ ТЕКСТ. Для регистрации сохраняются Telegram ID, согласия и история действий. До работы с гостями владелец должен утвердить документ и порядок удаления данных."),
            marketing_version=e.get("MARKETING_VERSION", "draft-marketing-1"),
            marketing_text=e.get("MARKETING_TEXT", "Я добровольно соглашаюсь получать рекламные сообщения ресторана в Telegram. Отказ не мешает пользоваться ботом. Отписаться: /unsubscribe."),
            menu_url=urls[0], delivery_url=urls[1], actions_url=urls[2],
            contacts=e.get("CONTACTS_TEXT", "Самара, ул. Лейтенанта Шмидта, 1В"), username=username,
            welcome_title=welcome_title, welcome_text=welcome_text,
        )

    def validate_launch(self):
        if not self.polling:
            raise ValueError("Polling выключен. Для локальных проверок: python -m unittest discover -s tests")
        # USER_ACTION: перевыпустить опубликованный токен и внести новый только в локальную .env.
        if not self.token or not self.admins:
            raise ValueError("Для запуска нужны BOT_TOKEN и ADMIN_IDS в окружении")
        if not self.sandbox and (not self.real_sends or not self.notices_approved):
            raise ValueError("Нужны отдельное разрешение ALLOW_REAL_SENDS и утверждённые NOTICES_APPROVED")
        if not self.sandbox and (not self.pd_text or not self.marketing_text or self.pd_version.startswith("draft") or self.marketing_version.startswith("draft")):
            raise ValueError("Замените черновики согласий утверждёнными текстами и версиями")
