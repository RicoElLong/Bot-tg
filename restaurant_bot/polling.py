"""Prevent python-telegram-bot's polling bootstrap from deleting a webhook."""
from telegram.ext import ExtBot


class PollingBot(ExtBot):
    async def require_no_webhook(self):
        info = await self.get_webhook_info()
        if info.url:
            raise ValueError("У бота уже настроен webhook. Polling остановлен без изменения настроек. Сначала согласуйте отключение предыдущей системы.")

    async def delete_webhook(self, drop_pending_updates=None, **_kwargs):
        if drop_pending_updates:
            raise ValueError("Удаление накопленных событий запрещено")
        await self.require_no_webhook()
        # Updater always calls this method, but no deleteWebhook request is sent.
        return True
