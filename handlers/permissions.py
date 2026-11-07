"""Роли и фильтры. Работа с ботом — только в личном диалоге; права проверяются на сервере."""
import logging

from aiogram import BaseMiddleware
from aiogram.filters import BaseFilter
from aiogram.types import Message

from services.bot_commands import sync_user_commands
from services.draft_context import get_ctx


log = logging.getLogger("mvauto")


def _chat(event):
    return event.chat if isinstance(event, Message) else (event.message.chat if event.message else None)


class PrivateOnly(BaseFilter):
    """Бот отвечает только в личных диалогах; в любых группах молчит."""

    async def __call__(self, event) -> bool:
        chat = _chat(event)
        return bool(chat and chat.type == "private")


PrivateChat = PrivateOnly


def role_of(user_id: int):
    return get_ctx().role(user_id)


def is_staff(user_id: int) -> bool:
    return role_of(user_id) in ("manager", "admin")


def is_admin(user_id: int) -> bool:
    return role_of(user_id) == "admin"


class AutoRegister(BaseMiddleware):
    """По ТЗ доступ только у заранее указанных ID, поэтому автодобавление выключено по умолчанию.
    /autoreg on делает менеджером любого, кто написал боту; отозванные через /removeuser не возвращаются."""

    async def __call__(self, handler, event, data):
        try:
            c = get_ctx()
            user, chat = event.from_user, _chat(event)
            if (user and not user.is_bot and chat and chat.type == "private" and not c.db.user_exists(user.id)
                    and c.db.get_setting("auto_register", False)):
                name = f"@{user.username}" if user.username else user.full_name
                c.db.set_user(user.id, "manager", name, added_by=0)
                c.db.audit(user.id, None, "auto_register", name)
                await sync_user_commands(c.bot, user.id, "manager")
        except Exception:
            log.exception("auto_register failed")
        return await handler(event, data)
