"""Telegram admin controls. Every entry point checks an explicit ID allowlist."""
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from .campaigns import audit


LABELS = {"sent":"Принято Telegram", "sandbox":"Имитация", "failed":"Ошибка", "uncertain":"Исход неизвестен", "pending":"В очереди", "sending":"Обрабатывается", "skipped":"Пропущено"}
STATES = {"draft":"Черновик", "queued":"В очереди", "running":"Отправляется", "done":"Завершена", "cancelled":"Отменена"}


class Admin:
    def __init__(self, config, store, campaigns, guest):
        self.config, self.store, self.campaigns, self.guest = config, store, campaigns, guest

    def authorized(self, update):
        return bool(update.effective_user and update.effective_user.id in self.config.admins and update.effective_chat and update.effective_chat.type == "private")

    async def panel(self, update, context):
        if not self.authorized(update):
            await self.guest.reply(update, context, "Нет доступа.")
            return
        context.user_data.pop("draft_waiting", None)
        with self.store.connect() as conn:
            audit(conn, update.effective_user.id, "admin_opened")
        await self.guest.reply(update, context, "Управление рестораном\nРежим: " + ("SANDBOX — отправок гостям нет" if self.config.sandbox else "РЕАЛЬНЫЕ ОТПРАВКИ") +
            "\nЧерновики и их отправка доступны только их автору.",
            Keyboard([[Button("Создать сообщение", callback_data="admin:new")], [Button("Статистика", callback_data="admin:stats"), Button("История", callback_data="admin:history")]]))

    async def cancel_input(self, update, context):
        if self.authorized(update):
            context.user_data.pop("draft_waiting", None)
            await self.guest.reply(update, context, "Ввод отменён. /admin")

    async def message(self, update, context):
        if not self.authorized(update) or not context.user_data.get("draft_waiting"):
            return
        msg = update.effective_message
        if msg.media_group_id:
            await self.guest.reply(update, context, "Альбом не поддерживается. Пришлите одно фото с подписью.")
            return
        text = msg.caption if msg.photo else msg.text
        photo = msg.photo[-1].file_id if msg.photo else None
        try:
            cid = self.campaigns.create(update.effective_user.id, text or "", photo)
        except ValueError as exc:
            await self.guest.reply(update, context, str(exc))
            return
        context.user_data.pop("draft_waiting", None)
        await self.preview(update, context, cid)

    async def preview(self, update, context, cid):
        campaign = self.campaigns.get(cid, update.effective_user.id)
        if not campaign:
            await self.guest.reply(update, context, "Черновик не найден или принадлежит другому администратору.")
            return
        result = await self.guest.reply(update, context, campaign["text"], photo=campaign["photo_id"])
        with self.store.connect() as conn:
            audit(conn, update.effective_user.id, "campaign_previewed", cid)
        if result not in {"sandbox", "sent"}:
            await self.guest.reply(update, context, "Предпросмотр не подтверждён Telegram. Массовая отправка не предложена. Создайте новый черновик после устранения ошибки.")
            return
        _, count, _, _ = self.store.stats()
        await self.guest.reply(update, context, f"Предпросмотр №{cid}: {result}.\nПодписчиков сейчас: {count}. Итоговая аудитория фиксируется при подтверждении.",
            Keyboard([[Button("Подтвердить массовую отправку", callback_data=f"admin:send:{cid}:{campaign['confirm_token']}")],
                      [Button("Отменить кампанию", callback_data=f"admin:cancel:{cid}")]]))

    async def callback(self, update, context):
        if not self.authorized(update):
            await self.guest.reply(update, context, "Нет доступа.")
            return
        await self.guest.gateway.answer(update.callback_query)
        parts = (update.callback_query.data or "").split(":")
        if len(parts) < 2:
            return
        action, uid = parts[1], update.effective_user.id
        if action == "new":
            context.user_data["draft_waiting"] = True
            await self.guest.reply(update, context, "Пришлите текст одним сообщением или одно фото с подписью. Альбомы не поддерживаются. Текст с фото — до 1024 символов. /cancel — отмена.")
        elif action == "stats":
            total, subscribed, counts, sandbox = self.store.stats()
            with self.store.connect() as conn:
                audit(conn, uid, "stats_viewed")
            report = "\n".join(f"{LABELS.get(k,k)}: {v}" for k,v in counts.items()) or "Рассылок пока нет."
            await self.guest.reply(update, context, f"Гостей: {total}\nС рекламным согласием: {subscribed}\n{report}\nВсего записей sandbox: {sandbox}\nПринято Telegram не означает прочтение.")
        elif action == "history":
            rows = self.campaigns.history(uid)
            lines = []
            for row in rows:
                counts = ", ".join(f"{LABELS.get(k,k)}: {v}" for k,v in row["counts"].items()) or "Получатели не выбраны"
                lines.append(f"№{row['id']}: {STATES.get(row['state'],row['state'])} · {row['created_at']}\n{counts}")
            buttons = [[Button(f"Предпросмотр №{r['id']}", callback_data=f"admin:preview:{r['id']}")] for r in rows if r["creator_id"] == uid and r["state"] == "draft"]
            await self.guest.reply(update, context, "Последние 10 кампаний (время UTC):\n" + ("\n\n".join(lines) or "Пока нет."), Keyboard(buttons) if buttons else None)
        elif action == "preview" and len(parts) == 3 and parts[2].isdigit():
            await self.preview(update, context, int(parts[2]))
        elif action in {"send", "cancel"} and len(parts) in {3,4} and parts[2].isdigit():
            cid = int(parts[2])
            if action == "send" and len(parts) == 4:
                count = self.campaigns.confirm(cid, uid, parts[3])
                await self.guest.reply(update, context, "Уже подтверждено, отменено или нет доступа." if count is None else f"Подтверждено. Получателей: {count}. " + ("Только имитация в sandbox." if self.config.sandbox else "Результаты — в статистике."))
            elif action == "cancel":
                changed = self.campaigns.cancel(cid, uid)
                await self.guest.reply(update, context, "Кампания отменена. Уже принятые Telegram сообщения не отзываются." if changed else "Кампания уже завершена или нет доступа.")
