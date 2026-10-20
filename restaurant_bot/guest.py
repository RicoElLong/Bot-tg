"""Private-chat registration, explicit consents and guest navigation."""
import re
import secrets
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard


def source_tag(value):
    return value if value and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) else "organic"


class Guest:
    def __init__(self, config, store, gateway):
        self.config, self.store, self.gateway = config, store, gateway

    async def reply(self, update, context, text, markup=None, photo=None):
        # Counters are reset by the update dispatcher and are scoped to one event.
        number = context.chat_data.get("reply_number", 0) + 1
        context.chat_data["reply_number"] = number
        return await self.gateway.send(update.effective_user.id, text,
            key=f"reply:{update.update_id}:{number}", photo=photo, markup=markup)

    def menu(self):
        return Keyboard([[Button("Акции", callback_data="guest:actions"), Button("Меню", callback_data="guest:menu")],
                         [Button("Доставка", callback_data="guest:delivery"), Button("Контакты", callback_data="guest:contacts")],
                         [Button("Настройки рассылки", callback_data="guest:subscribe")]])

    async def start(self, update, context):
        source = source_tag(context.args[0] if context.args else None)
        context.user_data["source"] = source
        if self.store.registered(update.effective_user.id):
            self.store.visit(update.effective_user.id, source)
            await self.reply(update, context, "С возвращением в ресторан «Дети лейтенанта Шмидта»!", self.menu())
            return
        await self.offer_pd(update, context)

    async def offer_pd(self, update, context):
        nonce = secrets.token_hex(8)
        context.user_data["pd_nonce"] = nonce
        await self.reply(update, context,
            "Добро пожаловать в ресторан «Дети лейтенанта Шмидта»!\n\nДля регистрации потребуется согласие на обработку данных. Это НЕ подписка на рекламу.\n\n" + self.config.pd_text + "\nВерсия: " + self.config.pd_version,
            Keyboard([[Button("Согласен на обработку данных", callback_data=f"pd:yes:{nonce}")], [Button("Не регистрироваться", callback_data=f"pd:no:{nonce}")]]))

    async def offer_marketing(self, update, context):
        if not self.store.registered(update.effective_user.id):
            await self.reply(update, context, "Сначала пройдите регистрацию: /start")
            return
        nonce = secrets.token_hex(8)
        context.user_data["marketing_nonce"] = nonce
        await self.reply(update, context, self.config.marketing_text + "\nВерсия: " + self.config.marketing_version,
            Keyboard([[Button("Да, получать рекламу", callback_data=f"mkt:yes:{nonce}")], [Button("Нет, без рекламы", callback_data=f"mkt:no:{nonce}")]]))

    async def unsubscribe(self, update, context):
        context.user_data.pop("marketing_nonce", None)
        self.store.consent(update.effective_user.id, False, self.config.marketing_version,
            self.config.marketing_text, "telegram:unsubscribe", str(update.update_id))
        await self.reply(update, context, "Рекламная подписка отключена. Уже отправленные сообщения отозвать нельзя. Меню и доставка доступны.", self.menu())

    async def privacy(self, update, context):
        await self.reply(update, context, self.config.pd_text + "\nВерсия: " + self.config.pd_version +
            "\n\n" + self.config.marketing_text + "\nВерсия: " + self.config.marketing_version +
            "\n\nОтказ от рекламы: /unsubscribe. Удаление профиля и отзыв согласия на обработку: /delete_me. В резервных копиях данные могут сохраняться до окончания утверждённого срока хранения.")

    async def help(self, update, context):
        await self.reply(update, context, "/start — регистрация и главное меню\n/subscribe — добровольная рекламная подписка\n/unsubscribe — отказаться от рекламы\n/profile — статус регистрации\n/privacy — согласия\n/delete_me — удалить профиль\n/help — помощь", self.menu())

    async def profile(self, update, context):
        uid = update.effective_user.id
        await self.reply(update, context, "Регистрация: " + ("есть" if self.store.registered(uid) else "нет") + "\nРекламная подписка: " + ("включена" if self.store.subscribed(uid) else "выключена"))

    async def delete_me(self, update, context):
        nonce = secrets.token_hex(8)
        context.user_data["delete_nonce"] = nonce
        await self.reply(update, context, "Удалить профиль, согласия и историю? Реклама прекратится. Для повторной регистрации потребуется новое согласие.",
            Keyboard([[Button("Подтверждаю удаление", callback_data=f"delete:{nonce}")]]))

    async def callback(self, update, context):
        query, uid = update.callback_query, update.effective_user.id
        data = query.data or ""
        await self.gateway.answer(query)
        parts = data.split(":")
        if parts[0] in {"pd", "mkt"} and len(parts) == 3:
            kind, answer, nonce = parts
            state_key = "pd_nonce" if kind == "pd" else "marketing_nonce"
            if answer not in {"yes", "no"} or context.user_data.get(state_key) != nonce:
                await self.reply(update, context, "Эта кнопка устарела. Используйте /start или /subscribe.")
                return
            context.user_data.pop(state_key, None)
            if kind == "pd":
                if answer == "no":
                    await self.reply(update, context, "Вы не зарегистрированы. Можно открыть меню и контакты без подписки.", self.menu())
                    return
                self.store.register(uid, context.user_data.get("source", "organic"), self.config.pd_version, self.config.pd_text, str(update.update_id))
                await self.offer_marketing(update, context)
            else:
                if not self.store.consent(uid, answer == "yes", self.config.marketing_version, self.config.marketing_text, "telegram:button", str(update.update_id)):
                    await self.reply(update, context, "Сначала зарегистрируйтесь: /start")
                    return
                await self.reply(update, context, "Готово! " + ("Рекламная подписка включена." if answer == "yes" else "Вы зарегистрированы без рекламы."), self.menu())
        elif parts[0] == "delete" and len(parts) == 2:
            if context.user_data.get("delete_nonce") != parts[1]:
                return
            await self.reply(update, context, "Профиль удаляется. Для нового согласия используйте /start.")
            self.store.delete(uid)
            context.user_data.clear()
        elif data == "guest:subscribe":
            await self.offer_marketing(update, context)
        elif data == "guest:contacts":
            await self.reply(update, context, self.config.contacts)
        elif data in {"guest:menu", "guest:delivery", "guest:actions"}:
            url = {"guest:menu": self.config.menu_url, "guest:delivery": self.config.delivery_url, "guest:actions": self.config.actions_url}[data]
            await self.reply(update, context, "Открыть раздел:" if url else "Раздел пока не подключён владельцем.", Keyboard([[Button("Открыть", url=url)]]) if url else None)
