"""Личные сообщения (только /start, /myid) и логирование добавления бота в группы."""
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated, Message

from handlers.permissions import PrivateChat

log = logging.getLogger("mvauto")
router = Router()


@router.message(Command("start", "myid"), PrivateChat())
async def cmd_start(m: Message):
    from handlers.menu import MENU_TEXT, menu_kb
    from handlers.permissions import is_admin, is_staff
    staff = is_staff(m.from_user.id)
    if staff and not m.text.startswith("/myid"):
        return await m.answer(MENU_TEXT, reply_markup=menu_kb(is_admin(m.from_user.id)))
    text = f"🪪 Ваш Telegram user_id: <code>{m.from_user.id}</code>"
    if not staff:
        text += "\n⛔ Доступа к боту нет. Передайте этот ID администратору."
    await m.answer(text)


@router.my_chat_member()
async def on_my_chat_member(u: ChatMemberUpdated):
    # Бот молчит в группах; chat_id пишем в лог, чтобы указать в .env
    log.info("Бот в чате: id=%s type=%s title=%r статус=%s", u.chat.id, u.chat.type, u.chat.title,
             u.new_chat_member.status)
