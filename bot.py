"""Точка входа: python bot.py"""
import asyncio
import logging
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import BASE_DIR, load_config
from database import Database
from handlers.permissions import AutoRegister
from handlers import history, manager_group, menu, photos, settings, start
from scheduler import PublishScheduler
from services.bot_commands import setup_commands
from services.draft_context import AppContext, seed_from_config, set_ctx


def setup_logging():
    (BASE_DIR / "data").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(), RotatingFileHandler(BASE_DIR / "data" / "bot.log", maxBytes=2_000_000,
                                                            backupCount=3, encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("aiogram").setLevel(logging.INFO)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.message.outer_middleware(AutoRegister())
    dp.callback_query.outer_middleware(AutoRegister())
    # порядок важен: команды настроек и меню раньше общего роутера ответов
    dp.include_routers(start.router, settings.router, menu.router, history.router, photos.router,
                       manager_group.router)
    return dp


async def main():
    setup_logging()
    log = logging.getLogger("mvauto")
    cfg = load_config()
    db = Database(cfg.db_path)
    seed_from_config(db, cfg)
    bot = Bot(cfg.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    ctx = AppContext(bot=bot, db=db, cfg=cfg)
    set_ctx(ctx)
    ctx.scheduler = PublishScheduler()
    ctx.scheduler.start()
    me = await bot.get_me()
    log.info("Бот @%s запущен. Рабочая группа: %s, продажа: %s, аукцион: %s", me.username,
             db.get_setting("managers_chat_id"), db.get_setting("sales_chat_id"), db.get_setting("auction_chat_id"))
    if not [u for u in db.list_users() if u["role"] == "admin"]:
        log.warning("Нет администраторов: укажите ADMIN_IDS в .env (user_id узнаётся командой /myid в личке бота).")
    try:
        await setup_commands(bot, db)
    except Exception:  # без панели бот всё равно работает
        log.exception("не удалось настроить панель команд")
    dp = build_dispatcher()
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query", "my_chat_member"])
    finally:
        ctx.scheduler.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
