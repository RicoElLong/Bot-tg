"""Formatting and transport boundaries; no Telegram connection or local .env."""
from dataclasses import replace
from html import unescape
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

from restaurant_bot.config import Config
from restaurant_bot.delivery import Gateway
from restaurant_bot.guest import Guest
from restaurant_bot.presentation import welcome
from restaurant_bot.storage import Store


class PresentationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cfg = Config.load({"MENU_URL": "https://example.org/menu", "DELIVERY_URL": "https://example.org/delivery"})
        self.store = Store(Path(temporary.name) / "test.sqlite3")
        self.bot = NS(send_message=AsyncMock(), send_photo=AsyncMock())
        self.gateway = Gateway(self.cfg, self.store, self.bot, sleep=AsyncMock())
        self.guest = Guest(self.cfg, self.store, self.gateway)
        self.update = NS(update_id=1, effective_user=NS(id=1))
        self.context = NS(args=[], user_data={}, chat_data={})

    async def test_custom_welcome_is_plain_text_and_html_is_escaped(self):
        cfg = Config.load({"WELCOME_TITLE": "Шмидт & гости", "WELCOME_TEXT": '<a href="https://evil.example">Здравствуйте</a>'})
        rendered = welcome(cfg)
        self.assertIn("Шмидт &amp; гости", rendered)
        self.assertNotIn('<a href=', rendered)
        self.assertIn("&lt;a href=", rendered)

    async def test_new_welcome_keeps_approved_document_and_does_not_register(self):
        document = "Обработка <данных> & контакты оператора."
        cfg = replace(self.cfg, pd_text=document)
        await Guest(cfg, self.store, self.gateway).start(self.update, self.context)
        with self.store.connect() as conn:
            payload = json.loads(conn.execute("SELECT payload FROM messages").fetchone()[0])
        self.assertIn(document, unescape(payload["text"]))
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertNotIn("Версия:", payload["text"])
        self.assertFalse(self.store.registered(1))
        self.bot.send_message.assert_not_awaited()

    async def test_configured_links_open_directly_and_empty_link_is_honest(self):
        keys = self.guest.menu().inline_keyboard
        self.assertEqual(keys[0][1].url, self.cfg.menu_url)
        self.assertEqual(keys[1][0].url, self.cfg.delivery_url)
        self.assertEqual(keys[0][0].callback_data, "guest:actions")
        self.update.callback_query = NS(data="guest:actions", answer=AsyncMock())
        await self.guest.callback(self.update, self.context)
        with self.store.connect() as conn:
            payload = json.loads(conn.execute("SELECT payload FROM messages").fetchone()[0])
        self.assertIn("пока не настроена", payload["text"])

    async def test_existing_menu_callback_still_opens_correct_link(self):
        self.update.callback_query = NS(data="guest:menu", answer=AsyncMock())
        await self.guest.callback(self.update, self.context)
        with self.store.connect() as conn:
            payload = json.loads(conn.execute("SELECT payload FROM messages").fetchone()[0])
        button = payload["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["text"], "Открыть меню")
        self.assertEqual(button["url"], self.cfg.menu_url)

    async def test_html_delivery_is_explicit_and_ads_remain_plain(self):
        gateway = Gateway(replace(self.cfg, sandbox=False, real_sends=True, polling=True), self.store, self.bot, sleep=AsyncMock())
        await gateway.send(1, "<b>Заголовок</b>", key="rich", parse_mode="HTML")
        self.assertEqual(self.bot.send_message.await_args.kwargs["parse_mode"], "HTML")
        await gateway.send(1, "<b>Текст автора</b>", key="plain", photo="test-photo")
        self.assertNotIn("parse_mode", self.bot.send_photo.await_args.kwargs)
        self.assertEqual(self.bot.send_photo.await_args.kwargs["caption"], "<b>Текст автора</b>")

    async def test_branding_change_does_not_change_consent_versions(self):
        self.store.pin_documents(self.cfg)
        changed = replace(self.cfg, welcome_title="Ресторан Шмидта", welcome_text="Новое приветствие")
        self.store.pin_documents(changed)
        self.assertEqual(changed.pd_version, self.cfg.pd_version)
        self.assertEqual(changed.marketing_version, self.cfg.marketing_version)

    async def test_welcome_photo_keeps_full_consent_in_separate_message(self):
        cfg = replace(self.cfg, welcome_photo="https://example.org/welcome.jpg")
        await Guest(cfg, self.store, self.gateway).start(self.update, self.context)
        with self.store.connect() as conn:
            rows = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM messages ORDER BY id")]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["photo_id"], cfg.welcome_photo)
        self.assertIsNone(rows[0]["reply_markup"])
        self.assertLessEqual(len(unescape(rows[0]["text"])), 1024)
        self.assertIsNone(rows[1]["photo_id"])
        self.assertIn(cfg.pd_text, unescape(rows[1]["text"]))
        self.assertFalse(self.store.registered(1))
        self.bot.send_photo.assert_not_awaited()

    async def test_returning_guest_gets_photo_and_menu_without_resubscription(self):
        self.store.register(1, "organic", "v1", "PD", "reg")
        cfg = replace(self.cfg, welcome_photo="file_id_example")
        await Guest(cfg, self.store, self.gateway).start(self.update, self.context)
        with self.store.connect() as conn:
            row = json.loads(conn.execute("SELECT payload FROM messages").fetchone()[0])
        self.assertEqual(row["photo_id"], cfg.welcome_photo)
        self.assertIn("inline_keyboard", row["reply_markup"])
        self.assertFalse(self.store.subscribed(1))

    async def test_failed_photo_falls_back_to_text_and_registration_stays_available(self):
        cfg = replace(self.cfg, welcome_photo="https://example.org/missing.jpg")
        gateway = NS(send=AsyncMock(side_effect=["failed", "sandbox"]))
        await Guest(cfg, self.store, gateway).start(self.update, self.context)
        self.assertEqual(gateway.send.await_count, 2)
        fallback = gateway.send.await_args
        self.assertIsNone(fallback.kwargs["photo"])
        self.assertIn("Начнём со знакомства?", fallback.args[1])
        self.assertIn("pd_nonce", self.context.user_data)

    async def test_welcome_photo_rejects_local_paths_and_insecure_links(self):
        for value in ("file:///tmp/photo.jpg", "http://example.org/photo.jpg", "welcome.jpg", "https://user:pass@example.org/photo.jpg"):
            with self.assertRaises(ValueError):
                Config.load({"WELCOME_PHOTO": value})
        self.assertEqual(Config.load({"WELCOME_PHOTO":"https://example.org/photo.jpg"}).welcome_photo,"https://example.org/photo.jpg")


if __name__ == "__main__":
    unittest.main()
