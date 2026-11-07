"""Показывает чаты, в которые добавлен бот (по последним обновлениям). Запускать при остановленном боте."""
import asyncio

from aiogram import Bot

from config import load_config


async def main():
    bot = Bot(load_config().token)
    me = await bot.get_me()
    print(f"Бот @{me.username}")
    seen = {}
    for u in await bot.get_updates(timeout=0, allowed_updates=["message", "my_chat_member", "callback_query"]):
        chat = (u.message or u.my_chat_member or (u.callback_query and u.callback_query.message))
        chat = getattr(chat, "chat", None)
        if chat:
            seen[chat.id] = (chat.type, chat.title or chat.username or chat.full_name)
    for cid, (t, title) in seen.items():
        print(f"{cid}\t{t}\t{title}")
    if not seen:
        print("Обновлений нет: напишите что-нибудь в рабочей группе (ответом боту) или удалите и заново добавьте бота.")
    await bot.session.close()

asyncio.run(main())
