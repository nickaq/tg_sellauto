"""Панель команд — кнопка рядом с полем ввода. Список зависит от роли и виден только в личном чате с ботом."""
import logging

from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat, MenuButtonCommands

from services.draft_context import get_ctx

log = logging.getLogger("mvauto")

GUEST = [("start", "Начать работу"), ("myid", "Узнать свой Telegram ID")]
STAFF = [
    ("menu", "Панель управления"),
    ("ai", "Создать пост с помощью ИИ"),
    ("new", "Создать пост вручную"),
    ("drafts", "Мои черновики"),
    ("scheduled", "Запланированные публикации"),
    ("history", "История публикаций"),
    ("help", "Инструкция"),
]
ADMIN = STAFF + [("settings", "Настройки"), ("users", "Пользователи")]


def _commands(items) -> list[BotCommand]:
    hide_ai = get_ctx().cfg.hide_ai
    return [BotCommand(command=c, description=d) for c, d in items if not (hide_ai and c == "ai")]


async def sync_user_commands(bot, user_id: int, role: str | None):
    """Панель конкретного человека. Без роли остаётся общий список для гостей."""
    scope = BotCommandScopeChat(chat_id=user_id)
    try:
        if role:
            await bot.set_my_commands(_commands(ADMIN if role == "admin" else STAFF), scope=scope)
        else:
            await bot.delete_my_commands(scope=scope)
    except Exception as ex:  # человек ещё не писал боту: панель появится после следующего запуска
        log.info("панель команд для %s не обновлена: %s", user_id, ex)


async def setup_commands(bot, db):
    await bot.set_my_commands(_commands(GUEST), scope=BotCommandScopeAllPrivateChats())
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    for u in db.list_users():
        await sync_user_commands(bot, u["user_id"], u["role"])
