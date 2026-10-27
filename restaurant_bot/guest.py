"""Private-chat registration, explicit consents and guest navigation."""
import re
import secrets
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from .presentation import brand, card, consent_document, welcome


def source_tag(value):
    return value if value and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) else "organic"


class Guest:
    def __init__(self, config, store, gateway):
        self.config, self.store, self.gateway = config, store, gateway

    async def reply(self, update, context, text, markup=None, photo=None, *, rich=False):
        # Counters are reset by the update dispatcher and are scoped to one event.
        number = context.chat_data.get("reply_number", 0) + 1
        context.chat_data["reply_number"] = number
        return await self.gateway.send(update.effective_user.id, text,
            key=f"reply:{update.update_id}:{number}", photo=photo, markup=markup,
            parse_mode="HTML" if rich else None)

    def menu(self):
        def link(label, url, callback):
            return Button(label, url=url) if url else Button(label, callback_data=callback)
        return Keyboard([[link("✦ Акции", self.config.actions_url, "guest:actions"), link("🍽 Меню", self.config.menu_url, "guest:menu")],
                         [link("🚘 Доставка", self.config.delivery_url, "guest:delivery"), Button("⌖ Контакты", callback_data="guest:contacts")]])

    def back(self):
        return Keyboard([[Button("← Главное меню", callback_data="guest:home")]])

    async def greeting_photo(self, update, context, text, markup=None):
        if not self.config.welcome_photo:
            return False
        result = await self.reply(update, context, text, markup, photo=self.config.welcome_photo, rich=True)
        # A failed or uncertain image must never prevent the consent flow.
        return result in {"sent", "sandbox"}

    async def home(self, update, context):
        await self.reply(update, context, brand(self.config) + "\n\nВыбирайте, с чего начнём: меню, доставка или встреча у Волги.", self.menu(), rich=True)

    async def start(self, update, context):
        source = source_tag(context.args[0] if context.args else None)
        context.user_data["source"] = source
        if self.store.registered(update.effective_user.id):
            self.store.visit(update.effective_user.id, source)
            text = brand(self.config) + "\n\nС возвращением на борт.\nРады снова видеть вас — выбирайте нужный раздел."
            if not await self.greeting_photo(update, context, text, self.menu()):
                await self.reply(update, context, text, self.menu(), rich=True)
            return
        await self.offer_pd(update, context)

    async def offer_pd(self, update, context):
        nonce = secrets.token_hex(8)
        context.user_data["pd_nonce"] = nonce
        with_photo = await self.greeting_photo(update, context, welcome(self.config))
        await self.reply(update, context,
            ("" if with_photo else welcome(self.config) + "\n\n") + consent_document("Начнём со знакомства?",
                "Для регистрации нужно ваше согласие на обработку данных. Рекламная подписка подключается отдельно и только по желанию.", self.config.pd_text),
            Keyboard([[Button("Согласен на обработку данных", callback_data=f"pd:yes:{nonce}")], [Button("Продолжить без регистрации", callback_data=f"pd:no:{nonce}")]]), rich=True)

    async def offer_marketing(self, update, context):
        if not self.store.registered(update.effective_user.id):
            await self.reply(update, context, "Сначала пройдите регистрацию: /start")
            return
        nonce = secrets.token_hex(8)
        context.user_data["marketing_nonce"] = nonce
        await self.reply(update, context, consent_document("Оставаться на связи?",
            "Хотите получать акции и рекламные предложения ресторана? Это добровольно: меню и доставка доступны при любом выборе.", self.config.marketing_text),
            Keyboard([[Button("Да, получать акции и рекламу", callback_data=f"mkt:yes:{nonce}")], [Button("Продолжить без рекламы", callback_data=f"mkt:no:{nonce}")]]), rich=True)

    async def unsubscribe(self, update, context):
        context.user_data.pop("marketing_nonce", None)
        self.store.consent(update.effective_user.id, False, self.config.marketing_version,
            self.config.marketing_text, "telegram:unsubscribe", str(update.update_id))
        await self.reply(update, context, card("Подписка отключена", "Новых рекламных сообщений не будет. Меню, доставка и контакты остаются под рукой.\n\nУже отправленные сообщения отозвать нельзя.\nВернуться к подписке: /subscribe."), self.menu(), rich=True)

    async def privacy(self, update, context):
        await self.reply(update, context, card("Ваши данные и согласия", self.config.pd_text + "\nВерсия: " + self.config.pd_version +
            "\n\n" + self.config.marketing_text + "\nВерсия: " + self.config.marketing_version +
            "\n\nОтказ от рекламы: /unsubscribe. Удаление профиля и отзыв согласия на обработку: /delete_me. В резервных копиях данные могут сохраняться до окончания утверждённого срока хранения."), self.back(), rich=True)

    async def help(self, update, context):
        await self.reply(update, context, card("Ваш ресторанный помощник", "/start — регистрация и главное меню\n/subscribe — добровольная рекламная подписка\n/unsubscribe — отказаться от рекламы\n/profile — статус регистрации\n/privacy — согласия\n/delete_me — удалить профиль\n/help — помощь"), self.menu(), rich=True)

    async def profile(self, update, context):
        uid = update.effective_user.id
        await self.reply(update, context, card("Ваш профиль", "Регистрация: " + ("завершена" if self.store.registered(uid) else "не пройдена") + "\nРекламная подписка: " + ("включена" if self.store.subscribed(uid) else "выключена") + "\n\nУправление подпиской: /subscribe\nОтказ от рекламы: /unsubscribe"), self.back(), rich=True)

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
                await self.reply(update, context, card("Откроем актуальное меню", "Этот выбор уже завершён или обновлён.\nГлавное меню: /start. Настройки подписки: /subscribe."), self.menu(), rich=True)
                return
            context.user_data.pop(state_key, None)
            if kind == "pd":
                if answer == "no":
                    await self.reply(update, context, card("Располагайтесь", "Продолжаем без регистрации и рекламной подписки. Меню, доставка и контакты доступны ниже."), self.menu(), rich=True)
                    return
                self.store.register(uid, context.user_data.get("source", "organic"), self.config.pd_version, self.config.pd_text, str(update.update_id))
                await self.offer_marketing(update, context)
            else:
                if not self.store.consent(uid, answer == "yes", self.config.marketing_version, self.config.marketing_text, "telegram:button", str(update.update_id)):
                    await self.reply(update, context, "Сначала зарегистрируйтесь: /start")
                    return
                text = "Рекламная подписка включена. Будем делиться предложениями ресторана.\nОтписаться можно в любой момент: /unsubscribe." if answer == "yes" else "Регистрация завершена без рекламной подписки. Если захотите получать предложения, используйте /subscribe."
                await self.reply(update, context, card("Добро пожаловать на борт", text + "\n\nВыбирайте нужный раздел:"), self.menu(), rich=True)
        elif parts[0] == "delete" and len(parts) == 2:
            if context.user_data.get("delete_nonce") != parts[1]:
                return
            await self.reply(update, context, "Профиль удаляется. Для нового согласия используйте /start.")
            self.store.delete(uid)
            context.user_data.clear()
        elif data == "guest:subscribe":
            await self.offer_marketing(update, context)
        elif data == "guest:home":
            await self.home(update, context)
        elif data == "guest:contacts":
            await self.reply(update, context, card("Встречаемся у Волги", self.config.contacts), self.back(), rich=True)
        elif data in {"guest:menu", "guest:delivery", "guest:actions"}:
            url = {"guest:menu": self.config.menu_url, "guest:delivery": self.config.delivery_url, "guest:actions": self.config.actions_url}[data]
            titles = {"guest:menu": ("Меню ресторана", "Открыть меню"), "guest:delivery": ("Ресторан с доставкой", "Заказать доставку"), "guest:actions": ("Предложения ресторана", "Смотреть акции")}
            title, label = titles[data]
            buttons = [[Button(label, url=url)], [Button("← Главное меню", callback_data="guest:home")]] if url else self.menu().inline_keyboard
            await self.reply(update, context, card(title, "Перейдите по кнопке ниже." if url else "Ссылка на этот раздел пока не настроена. Остальные разделы доступны в меню."), Keyboard(buttons), rich=True)
