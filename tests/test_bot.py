import asyncio
from dataclasses import replace
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from telegram import Update
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut
from restaurant_bot.admin import Admin
from restaurant_bot.app import build_application
from restaurant_bot.backup import backup_database, restore_database
from restaurant_bot.campaigns import Campaigns
from restaurant_bot.config import Config
from restaurant_bot.delivery import Gateway, run_recipient
from restaurant_bot.guest import Guest, source_tag
from restaurant_bot.instance import instance_lock
from restaurant_bot.links import deep_link
from restaurant_bot.polling import PollingBot
from restaurant_bot.storage import Store


class Fixture:
    def setup_fixture(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        self.cfg = Config.load({"ADMIN_IDS": "100", "BOT_TOKEN": "123:unit-test"})
        self.campaigns = Campaigns(self.store)

    def register(self, uid=1, marketing=True):
        self.store.register(uid, "qr_table_1", "pd-v1", "PD text", f"reg-{uid}")
        if marketing is not None:
            self.store.consent(uid, marketing, "mkt-v1", "Marketing text", "telegram:button", f"mkt-{uid}")

    def queued(self):
        cid = self.campaigns.create(100, "Сегодня акция", "test-photo")
        row = self.campaigns.get(cid, 100)
        self.campaigns.confirm(cid, 100, row["confirm_token"])
        return cid


class StorageTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup_fixture()

    def test_default_network_and_sends_disabled(self):
        self.assertTrue(self.cfg.sandbox)
        self.assertFalse(self.cfg.polling)
        with self.assertRaises(ValueError):
            self.cfg.validate_launch()

    def test_explicit_gates_required_for_live(self):
        with self.assertRaises(ValueError):
            replace(self.cfg, sandbox=False, polling=True).validate_launch()
        replace(self.cfg, sandbox=False, polling=True, real_sends=True, notices_approved=True,
                pd_version="pd-v1", marketing_version="mkt-v1").validate_launch()

    def test_config_rejects_invalid_flags_ids_and_links(self):
        for env in ({"SANDBOX":"no"}, {"ADMIN_IDS":"abc"}, {"ADMIN_IDS":"-1"}, {"MENU_URL":"javascript:alert(1)"}, {"MENU_URL":"https://user:pass@example.org/"}):
            with self.assertRaises(ValueError):
                Config.load(env)

    def test_registration_is_unique_not_marketing(self):
        self.register(marketing=None)
        self.register(marketing=None)
        self.assertTrue(self.store.registered(1))
        self.assertFalse(self.store.subscribed(1))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM clients").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM consents").fetchone()[0], 1)

    def test_consents_have_version_time_source_and_snapshot(self):
        self.register()
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM consents WHERE kind='marketing'").fetchone()
            self.assertEqual(row["version"], "mkt-v1")
            self.assertEqual(row["document"], "Marketing text")
            self.assertTrue(row["occurred_at"].endswith("+00:00"))
            self.assertEqual(row["source"], "telegram:button")

    def test_unsubscribe_is_latest_and_idempotent(self):
        self.register()
        for _ in range(2):
            self.store.consent(1, False, "mkt-v1", "Marketing text", "unsubscribe", "event2")
        self.assertFalse(self.store.subscribed(1))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM consents WHERE kind='marketing'").fetchone()[0], 2)

    def test_no_marketing_consent_without_registration(self):
        self.assertFalse(self.store.consent(1, True, "v1", "text", "button", "e1"))

    def test_attribution_validation_and_repeat_visits(self):
        self.assertEqual(source_tag("qr_table-01"), "qr_table-01")
        for value in ("<script>", "x"*65, "привет", "", None):
            self.assertEqual(source_tag(value), "organic")
        self.register()
        self.store.visit(1, "qr_door")
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM clients").fetchone()
            self.assertEqual(row["first_source"], "qr_table_1")
            self.assertEqual(row["last_source"], "qr_door")

    def test_confirmation_author_token_and_duplicate_protection(self):
        self.register()
        self.register(2, False)
        self.register(3, None)
        cid = self.campaigns.create(100, "Hello")
        token = self.campaigns.get(cid, 100)["confirm_token"]
        self.assertIsNone(self.campaigns.get(cid, 200))
        self.assertIsNone(self.campaigns.confirm(cid, 200, token))
        self.assertIsNone(self.campaigns.confirm(cid, 100, "wrong"))
        self.assertEqual(self.campaigns.confirm(cid, 100, token), 1)
        self.assertIsNone(self.campaigns.confirm(cid, 100, token))

    def test_message_limits(self):
        for text, photo in (("", None), ("x"*4097, None), ("x"*1025, "photo")):
            with self.assertRaises(ValueError):
                self.campaigns.create(100, text, photo)
        self.campaigns.create(100, "x"*4096)

    def test_cancellation_skips_pending(self):
        self.register()
        cid = self.queued()
        self.assertFalse(self.campaigns.cancel(cid, 200))
        self.assertTrue(self.campaigns.cancel(cid, 100))
        self.assertIsNone(self.campaigns.next_recipient())

    def test_crash_recovery_does_not_repeat_claimed_send(self):
        self.register()
        self.queued()
        row = self.campaigns.next_recipient()
        self.assertTrue(self.campaigns.claim(row["campaign_id"], row["recipient_key"]))
        self.campaigns.recover()
        self.assertIsNone(self.campaigns.next_recipient())
        self.assertEqual(self.store.stats()[2], {"uncertain":1})

    def test_sandbox_database_cannot_be_used_live(self):
        self.store.bind_mode("sandbox")
        self.store.bind_mode("sandbox")
        with self.assertRaises(ValueError):
            self.store.bind_mode("live")

    def test_update_deduplication_survives_restart(self):
        self.assertTrue(self.store.claim_update(12))
        self.store.complete_update(12)
        self.assertFalse(Store(self.store.path).claim_update(12))

    def test_document_version_cannot_silently_change(self):
        self.store.pin_documents(self.cfg)
        self.store.pin_documents(self.cfg)
        with self.assertRaises(ValueError):
            self.store.pin_documents(replace(self.cfg, pd_text="Changed"))
        self.store.pin_documents(replace(self.cfg, pd_text="Changed", pd_version="pd-v2"))

    def test_unknown_database_is_not_overwritten(self):
        path = Path(self.temp.name) / "foreign.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE important(value TEXT)")
        conn.close()
        with self.assertRaises(ValueError):
            Store(path)

    def test_link_for_qr_has_original_source(self):
        self.assertEqual(deep_link("my_restaurant_bot", "qr_table_01"), "https://t.me/my_restaurant_bot?start=qr_table_01")
        for name, source in (("", "qr"), ("my_bot", "bad source")):
            with self.assertRaises(ValueError):
                deep_link(name, source)

    def test_instance_lock_rejects_second_poller(self):
        with instance_lock(self.store.path):
            with self.assertRaises(RuntimeError):
                with instance_lock(self.store.path):
                    pass

    def test_online_backup_restore_integrity_and_no_overwrite(self):
        self.register()
        self.queued()
        dest = Path(self.temp.name) / "backup.sqlite3"
        backup_database(self.store.path, dest)
        with sqlite3.connect(dest) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT count(*) FROM clients").fetchone()[0], 1)
        with self.assertRaises(FileExistsError):
            backup_database(self.store.path, dest)
        restored = Path(self.temp.name) / "restore.sqlite3"
        restore_database(dest, restored)
        with sqlite3.connect(restored) as conn:
            self.assertEqual(conn.execute("SELECT state FROM campaigns").fetchone()[0], "cancelled")
            self.assertEqual(conn.execute("SELECT status FROM recipients").fetchone()[0], "skipped")

    def test_delete_removes_profile_consents_and_does_not_recreate_recipient(self):
        self.register()
        self.queued()
        self.store.delete(1)
        self.assertFalse(self.store.registered(1))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM consents").fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT client_id FROM recipients").fetchone()[0])
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])


class AsyncTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_fixture()
        self.no_network = patch("httpx.AsyncClient.request", side_effect=AssertionError("External network forbidden in tests"))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)
        self.bot = NS(send_message=AsyncMock(), send_photo=AsyncMock())
        self.sleep = AsyncMock()
        self.gateway = Gateway(self.cfg, self.store, self.bot, self.sleep)
        self.guest = Guest(self.cfg, self.store, self.gateway)
        self.admin = Admin(self.cfg, self.store, self.campaigns, self.guest)
        self.context = NS(args=[], user_data={}, chat_data={})

    def update(self, uid=1, data=None, number=1000):
        return NS(effective_user=NS(id=uid), effective_chat=NS(type="private", id=uid), update_id=number,
                  callback_query=NS(data=data, answer=AsyncMock()) if data else None)

    def live_gateway(self):
        return Gateway(replace(self.cfg, sandbox=False, real_sends=True, polling=True), self.store, self.bot, self.sleep)

    async def test_sandbox_text_photo_and_dedup_never_call_api(self):
        for _ in range(2):
            self.assertEqual(await self.gateway.send(1, "test", key="k", photo="p"), "sandbox")
        self.bot.send_message.assert_not_awaited()
        self.bot.send_photo.assert_not_awaited()
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 1)

    async def test_sandbox_campaign_and_statistics(self):
        self.register()
        self.queued()
        self.assertTrue(await run_recipient(self.campaigns, self.gateway))
        self.assertFalse(await run_recipient(self.campaigns, self.gateway))
        self.assertEqual(self.store.stats()[2], {"sandbox":1})
        self.bot.send_photo.assert_not_awaited()

    async def test_unsubscribe_after_confirmation_skips_guest(self):
        self.register()
        self.queued()
        self.store.consent(1, False, "v1", "text", "unsubscribe", "new-event")
        await run_recipient(self.campaigns, self.gateway)
        self.assertEqual(self.store.stats()[2], {"skipped":1})

    async def test_live_send_success_persists_claim_before_api(self):
        async def check(**kwargs):
            with self.store.connect() as conn:
                self.assertEqual(conn.execute("SELECT status FROM messages WHERE delivery_key='live'").fetchone()[0], "sending")
        self.bot.send_message.side_effect = check
        self.assertEqual(await self.live_gateway().send(1,"text",key="live"), "sent")
        self.bot.send_message.assert_awaited_once()

    async def test_retry_after_only_retries_explicit_rejection(self):
        self.bot.send_message.side_effect = [RetryAfter(2), NS(message_id=1)]
        self.assertEqual(await self.live_gateway().send(1,"text",key="retry"), "sent")
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertIn(unittest.mock.call(2.0), self.sleep.await_args_list)

    async def test_timeout_is_uncertain_and_never_retried(self):
        self.bot.send_message.side_effect = TimedOut()
        live = self.live_gateway()
        self.assertEqual(await live.send(1,"text",key="timeout"), "uncertain")
        self.assertEqual(await live.send(1,"text",key="timeout"), "uncertain")
        self.bot.send_message.assert_awaited_once()

    async def test_forbidden_blocks_recipient(self):
        self.register()
        self.bot.send_message.side_effect = Forbidden("blocked")
        self.assertEqual(await self.live_gateway().send(1,"text",key="blocked"), "failed")
        self.assertEqual(self.store.stats()[1], 0)

    async def test_bad_request_is_not_retried(self):
        self.bot.send_photo.side_effect = BadRequest("bad photo")
        self.assertEqual(await self.live_gateway().send(1,"text",photo="p",key="bad"), "failed")
        self.bot.send_photo.assert_awaited_once()

    async def test_long_flood_wait_fails_without_blocking_shared_gateway(self):
        self.bot.send_message.side_effect = RetryAfter(600)
        live = self.live_gateway()
        self.assertEqual(await live.send(1,"text",key="long-wait"), "failed")
        self.assertFalse(live.lock.locked())
        self.bot.send_message.assert_awaited_once()
        self.assertFalse(any(call.args == (600.0,) for call in self.sleep.await_args_list))

    async def test_removed_admin_cannot_continue_pending_campaign(self):
        self.register()
        self.queued()
        gateway = Gateway(replace(self.cfg, admins=frozenset()), self.store, self.bot)
        await run_recipient(self.campaigns, gateway)
        self.assertEqual(self.store.stats()[2], {"skipped":1})

    async def test_consent_is_rechecked_after_rate_wait(self):
        allowed = True
        async def revoke(_seconds):
            nonlocal allowed
            allowed = False
        gateway = Gateway(replace(self.cfg, sandbox=False, real_sends=True, polling=True), self.store, self.bot, revoke)
        self.assertEqual(await gateway.send(1,"text",key="revoke",permitted=lambda:allowed), "skipped")
        self.bot.send_message.assert_not_awaited()

    async def test_start_requires_pd_then_separate_optional_marketing(self):
        self.context.args = ["qr_window"]
        await self.guest.start(self.update(), self.context)
        self.assertFalse(self.store.registered(1))
        nonce = self.context.user_data["pd_nonce"]
        await self.guest.callback(self.update(data=f"pd:yes:{nonce}", number=1001), self.context)
        self.assertTrue(self.store.registered(1))
        self.assertFalse(self.store.subscribed(1))
        nonce = self.context.user_data["marketing_nonce"]
        await self.guest.callback(self.update(data=f"mkt:no:{nonce}", number=1002), self.context)
        self.assertTrue(self.store.registered(1))
        self.assertFalse(self.store.subscribed(1))
        await self.guest.start(self.update(number=1003), self.context)
        self.assertEqual(self.store.stats()[0], 1)

    async def test_stale_marketing_button_cannot_resubscribe_after_stop(self):
        self.register()
        await self.guest.offer_marketing(self.update(), self.context)
        nonce = self.context.user_data["marketing_nonce"]
        await self.guest.unsubscribe(self.update(number=1001), self.context)
        await self.guest.callback(self.update(data=f"mkt:yes:{nonce}", number=1002), self.context)
        self.assertFalse(self.store.subscribed(1))

    async def test_guest_decline_does_not_create_profile(self):
        await self.guest.start(self.update(), self.context)
        nonce = self.context.user_data["pd_nonce"]
        await self.guest.callback(self.update(data=f"pd:no:{nonce}", number=1001), self.context)
        self.assertEqual(self.store.stats()[0], 0)

    async def test_admin_denied_for_unknown_id_and_groups(self):
        self.assertFalse(self.admin.authorized(self.update(1)))
        group = self.update(100)
        group.effective_chat.type = "group"
        self.assertFalse(self.admin.authorized(group))
        await self.admin.callback(self.update(data="admin:new"), self.context)
        self.assertNotIn("draft_waiting", self.context.user_data)
        await self.admin.message(NS(effective_user=NS(id=1), effective_chat=NS(type="private")), self.context)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM campaigns").fetchone()[0], 0)

    async def test_admin_photo_preview_confirm_and_audit(self):
        self.register()
        await self.admin.callback(self.update(100,"admin:new"), self.context)
        u = self.update(100,number=1001)
        u.effective_message = NS(photo=[NS(file_id="photo-test")],caption="Скидка",text=None,media_group_id=None)
        await self.admin.message(u, self.context)
        campaign = self.campaigns.get(1,100)
        self.assertEqual(campaign["state"], "draft")
        self.assertIsNone(self.campaigns.next_recipient())
        await self.admin.callback(self.update(100,f"admin:send:1:{campaign['confirm_token']}",1002),self.context)
        self.assertIsNotNone(self.campaigns.next_recipient())
        with self.store.connect() as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT action FROM audit")], ["campaign_created","campaign_previewed","campaign_confirmed"])

    async def test_webhook_is_preserved_and_pending_updates_not_dropped(self):
        bot = PollingBot("123:unit-test")
        with patch.object(PollingBot,"get_webhook_info",new=AsyncMock(return_value=NS(url="https://existing.example/hook"))):
            with self.assertRaises(ValueError):
                await bot.delete_webhook()
        with patch.object(PollingBot,"get_webhook_info",new=AsyncMock(return_value=NS(url=""))):
            self.assertTrue(await bot.delete_webhook())
            with self.assertRaises(ValueError):
                await bot.delete_webhook(drop_pending_updates=True)

    async def test_application_routes_start_and_replay_without_network(self):
        app = build_application(self.cfg,self.store)
        object.__setattr__(app,"_initialized",True)
        object.__setattr__(app.bot,"_bot_user", NS(id=123,username="test_bot"))
        for _ in range(2):
            u = Update.de_json({"update_id":3000,"message":{"message_id":1,"date":0,"chat":{"id":1,"type":"private"},"from":{"id":1,"is_bot":False,"first_name":"Test"},"text":"/start qr_table","entities":[{"type":"bot_command","offset":0,"length":6}]}},app.bot)
            await app.process_update(u)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT status FROM processed_updates").fetchone()[0],"done")
        await app.bot.shutdown()

    async def test_full_registration_admin_campaign_and_unsubscribe_via_routes(self):
        app = build_application(self.cfg,self.store)
        object.__setattr__(app,"_initialized",True)
        object.__setattr__(app.bot,"_bot_user", NS(id=123,username="test_bot"))
        counter = 5000

        async def send(uid, text=None, callback=None):
            nonlocal counter
            counter += 1
            message = {"message_id":counter,"date":0,"chat":{"id":uid,"type":"private"},"from":{"id":uid,"is_bot":False,"first_name":"Test"},"text":text or "button"}
            if text and text.startswith("/"):
                message["entities"] = [{"type":"bot_command","offset":0,"length":len(text.split()[0])}]
            body = {"update_id":counter}
            if callback:
                body["callback_query"] = {"id":str(counter),"from":message["from"],"chat_instance":"unit","data":callback,"message":message}
            else:
                body["message"] = message
            await app.process_update(Update.de_json(body,app.bot))

        await send(1,"/start qr_menu")
        await send(1,callback="pd:yes:" + app.user_data[1]["pd_nonce"])
        await send(1,callback="mkt:yes:" + app.user_data[1]["marketing_nonce"])
        self.assertTrue(self.store.subscribed(1))
        await send(100,"/admin")
        await send(100,callback="admin:new")
        await send(100,"Специальное предложение")
        campaign = self.campaigns.get(1,100)
        self.assertIsNotNone(campaign)
        self.assertEqual(campaign["state"],"draft")
        await send(100,callback="admin:send:1:" + campaign["confirm_token"])
        await send(1,"/unsubscribe")
        await run_recipient(app.bot_data["campaigns"],app.bot_data["gateway"])
        self.assertEqual(self.store.stats()[2],{"skipped":1})
        self.assertFalse(self.store.subscribed(1))
        await app.bot.shutdown()


if __name__ == "__main__":
    unittest.main()
