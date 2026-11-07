"""Карточки черновиков, клавиатуры и вопросы бота."""
import logging
from datetime import datetime

from aiogram.exceptions import TelegramBadRequest
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup, InputMediaPhoto

import database as dbm
from services.draft_context import get_ctx
from services.post_generator import build_post, esc
from services.validation import PHOTOS_REQUIRED, caption_overflow, missing_required, overflow_text

log = logging.getLogger("mvauto")

STATUS_RU = {
    dbm.DRAFT: "✏️ Черновик", dbm.PENDING: "⏳ На согласовании", dbm.CHANGES: "↩️ Возвращён на доработку",
    dbm.APPROVED: "✅ Подтверждён", dbm.SCHEDULED: "🕒 Запланирован", dbm.PUBLISHING: "📤 Публикуется",
    dbm.PUBLISHED: "📣 Опубликован", dbm.REJECTED: "⛔ Отклонён", dbm.CANCELLED: "🚫 Отменён",
    dbm.FAILED: "⚠️ Ошибка публикации",
}
TYPE_RU = {"sales": "🚗 Автомобиль на продажу", "auction": "🇺🇸 Автомобиль с аукциона США/Канады"}
GROUP_RU = {"sales": "№1 Актуальные авто", "auction": "№2 Аукционы"}
FIELD_ICONS = {
    "color": "🎨", "model": "🚗", "subtitle": "🏷", "year": "📆", "drive": "⚙️", "engine": "🔧", "power": "⚡",
    "gearbox": "🕹", "mileage": "🛣", "price_ge": "💵", "price_ru": "💶", "ps": "📝", "tag": "🔖",
    "safety": "🛡", "equipment": "🧰", "auction_date": "📅", "bid": "💰",
}


def fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    return datetime.fromisoformat(iso).astimezone(get_ctx().tz).strftime("%d.%m.%Y %H:%M")


def render_card(d: dict) -> str:
    c = get_ctx()
    n = len(c.db.photos(d["id"]))
    photos_left = f" (осталось {PHOTOS_REQUIRED - n})" if n < PHOTOS_REQUIRED else " ✅"
    lines = [
        f"📋 <b>Публикация {d['code']}</b> — автор: {esc(d['creator_name'] or d['creator_id'])}",
        f"{TYPE_RU[d['type']]} → группа {GROUP_RU[d['type']]}",
        f"🚘 Авто: {esc(d['fields'].get('model') or '—')}",
        f"📷 Фото: {n}/{PHOTOS_REQUIRED}{photos_left}",
        f"📌 Статус: {STATUS_RU.get(d['status'], d['status'])} · версия {d['version']}",
    ]
    miss = missing_required(d["type"], d["fields"])
    if miss and d["status"] in (dbm.DRAFT, dbm.CHANGES):
        lines.append("❗ Не заполнено: " + ", ".join(esc(f.label) for f in miss))
    over = caption_overflow(d, c.settings_for_post())
    if over and d["status"] in (dbm.DRAFT, dbm.CHANGES):
        lines.append("❗ " + esc(overflow_text(over)))
    if d["status"] == dbm.SCHEDULED:
        lines.append(f"🕒 Публикация: {fmt_dt(d['scheduled_at'])} ({c.tz.key})")
    if d.get("approved_by_name") and d["status"] in (dbm.APPROVED, dbm.SCHEDULED, dbm.PUBLISHED):
        lines.append(f"👍 Подтвердил: {esc(d['approved_by_name'])}")
    if d["status"] in (dbm.CHANGES, dbm.REJECTED) and d.get("reject_reason"):
        lines.append(f"💬 Комментарий: {esc(d['reject_reason'])}")
    if d["status"] == dbm.FAILED and d.get("error"):
        lines.append(f"⚠️ Ошибка: {esc(d['error'][:300])}")
    return "\n".join(lines)


def card_keyboard(d: dict) -> InlineKeyboardMarkup | None:
    i, v, s = d["id"], d["version"], d["status"]

    def b(text, act):
        return B(text=text, callback_data=f"d:{act}:{i}:{v}")
    rows = []
    if s in (dbm.DRAFT, dbm.CHANGES, dbm.PENDING):
        rows = [[b("✏️ Редактировать", "edit"), b("📷 Фото", "ph")],
                [b("✅ Подтвердить и опубликовать", "sub")],
                [b("👁 Предпросмотр", "pv"), b("🕒 Запланировать", "aps")],
                [b("🗑 Отменить", "cancel")]]
    elif s == dbm.APPROVED:
        rows = [[b("🚀 Опубликовать сейчас", "now"), b("🕒 Запланировать", "sch")],
                [b("👁 Предпросмотр", "pv"), b("✏️ Редактировать", "edit")],
                [b("🗑 Отменить", "cancel")]]
    elif s == dbm.SCHEDULED:
        rows = [[b("🕒 Перенести", "sch"), b("⏸ Снять с расписания", "uns")],
                [b("👁 Предпросмотр", "pv"), b("✏️ Редактировать", "edit")],
                [b("🗑 Отменить публикацию", "cancel")]]
    elif s == dbm.FAILED:
        rows = [[b("🔁 Повторить публикацию", "retry")]]
        pub = get_ctx().db.get_publication(i)
        if not (pub and pub["album_ids"]):  # альбом ещё не ушёл — данные можно исправить
            rows.append([b("✏️ Вернуть в черновик", "fix")])
        rows.append([b("🗑 Отменить", "cancel")])
        rows.append([b("👁 Предпросмотр", "pv")])
    else:
        rows = [[b("👁 Предпросмотр", "pv")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def refresh_card(draft_id: int, new: bool = False):
    """Обновить карточку (редактированием) или опубликовать новую и убрать старую."""
    c = get_ctx()
    d = c.db.get_draft(draft_id)
    text, kb = render_card(d), card_keyboard(d)
    chat = d["manager_chat_id"]
    if d["card_message_id"] and not new:
        try:
            await c.bot.edit_message_text(text, chat_id=chat, message_id=d["card_message_id"], reply_markup=kb)
            return
        except TelegramBadRequest as ex:
            if "not modified" in str(ex):
                return
    old = d["card_message_id"]
    m = await c.bot.send_message(chat, text, reply_markup=kb)
    c.db.update_draft(draft_id, card_message_id=m.message_id)
    if old:
        try:
            await c.bot.delete_message(chat, old)
        except Exception:
            try:
                await c.bot.edit_message_reply_markup(chat_id=chat, message_id=old, reply_markup=None)
            except Exception:
                pass


async def send_card(d: dict, chat_id: int):
    """Отдельная карточка в чате (например, когда черновик открыл администратор, а не автор)."""
    return await get_ctx().bot.send_message(chat_id, render_card(d), reply_markup=card_keyboard(d))


async def show_card(d: dict, chat_id: int):
    """Свежая карточка внизу диалога: у автора заменяет старую, у администратора — отдельная."""
    if chat_id == d["manager_chat_id"]:
        await refresh_card(d["id"], new=True)
    else:
        await send_card(d, chat_id)


async def ask_field(d: dict, f, chat_id: int | None = None):
    """Вопрос по одному полю в чат того, кто редактирует; ответ — следующим сообщением."""
    c = get_ctx()
    chat_id = chat_id or d["manager_chat_id"]
    icon = FIELD_ICONS.get(f.key, "📝")
    text = (f"{icon} <b>{d['code']}</b> · {esc(f.label)}\n{esc(f.prompt)}\n<i>💡 Пример: {esc(f.example)}</i>\n\n"
            "✍️ Просто напишите ответ сообщением.")
    kb = None
    if not f.required:
        kb = InlineKeyboardMarkup(inline_keyboard=[[B(text="⏭ Пропустить / очистить",
                                                       callback_data=f"d:sk:{d['id']}:{d['version']}")]])
    m = await c.bot.send_message(chat_id, text, reply_markup=kb)
    c.db.add_prompt(chat_id, m.message_id, d["id"], "field", f.key, replace_kinds=("field", "schedule"))


def photo_prompt_text(d: dict, n: int) -> str:
    if n == PHOTOS_REQUIRED:
        state = f"✅ Все {PHOTOS_REQUIRED} фото получены. Нажмите «Завершить загрузку»."
    elif n < PHOTOS_REQUIRED:
        state = f"⏳ Осталось добавить: <b>{PHOTOS_REQUIRED - n}</b>."
    else:
        state = f"⚠️ Лишних фото: <b>{n - PHOTOS_REQUIRED}</b>, удалите их."
    extra = ""
    if d.get("replace_pos"):
        extra = f"\n🔄 Режим замены: следующее присланное фото заменит фото №{d['replace_pos']}."
    return (f"📷 <b>Фото для {d['code']}</b>\nОтправьте ровно {PHOTOS_REQUIRED} фото (по одному или альбомом).\n"
            f"🖼 Загружено: <b>{n}/{PHOTOS_REQUIRED}</b>. {state}{extra}")


def photo_prompt_kb(d: dict, mode: str = "main") -> InlineKeyboardMarkup:
    c = get_ctx()
    i = d["id"]
    n = len(c.db.photos(i))
    if mode in ("del", "rep"):
        act = "phdel" if mode == "del" else "phrep"
        icon = "🗑" if mode == "del" else "🔄"
        rows, row = [], []
        for k in range(1, n + 1):
            row.append(B(text=f"{icon} №{k}", callback_data=f"d:{act}:{i}:0:{k}"))
            if len(row) == 5:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([B(text="⬅️ Назад", callback_data=f"d:phb:{i}:0")])
        return InlineKeyboardMarkup(inline_keyboard=rows)
    rows = [[B(text="🔄 Заменить", callback_data=f"d:phr:{i}:0"), B(text="🗑 Удалить", callback_data=f"d:phd:{i}:0"),
             B(text="🧹 Очистить все", callback_data=f"d:phc:{i}:0")]]
    if n == PHOTOS_REQUIRED:
        rows.insert(0, [B(text="✅ Завершить загрузку", callback_data=f"d:phdone:{i}:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_photo_prompt(d: dict, chat_id: int | None = None):
    c = get_ctx()
    chat_id = chat_id or d["manager_chat_id"]
    n = len(c.db.photos(d["id"]))
    m = await c.bot.send_message(chat_id, photo_prompt_text(d, n), reply_markup=photo_prompt_kb(d))
    c.db.add_prompt(chat_id, m.message_id, d["id"], "photos", replace_kinds=("photos",))


async def update_photo_prompt(draft_id: int):
    c = get_ctx()
    d = c.db.get_draft(draft_id)
    p = c.db.active_prompt(draft_id, "photos")
    if not p:
        return
    try:
        await c.bot.edit_message_text(photo_prompt_text(d, len(c.db.photos(draft_id))),
                                      chat_id=p["chat_id"], message_id=p["message_id"],
                                      reply_markup=photo_prompt_kb(d))
    except TelegramBadRequest:
        pass


async def repost_photo_prompt(draft_id: int):
    """Вопрос про фото заново внизу диалога: после альбома старый уезжает за экран вместе с кнопкой."""
    c = get_ctx()
    d = c.db.get_draft(draft_id)
    p = c.db.active_prompt(draft_id, "photos")
    if not p:
        return
    await send_photo_prompt(d, p["chat_id"])
    try:
        await c.bot.edit_message_reply_markup(chat_id=p["chat_id"], message_id=p["message_id"], reply_markup=None)
    except TelegramBadRequest:
        pass


def album_media(photos: list, caption: str | None = None) -> list[InputMediaPhoto]:
    """Альбом как один пост: текст — подпись к первому фото, тогда Telegram показывает его под всем альбомом."""
    media = [InputMediaPhoto(media=p["file_id"]) for p in photos]
    if caption:
        media[0] = InputMediaPhoto(media=photos[0]["file_id"], caption=caption, parse_mode=ParseMode.HTML)
    return media


async def with_emoji_fallback(send):
    """Отправка с анимированными эмодзи; если Telegram их отверг — с обычными. send(custom_emoji: bool)."""
    try:
        return await send(True)
    except TelegramBadRequest:
        return await send(False)


async def send_post_text(d: dict, chat_id: int):
    c = get_ctx()
    settings = c.settings_for_post()
    return await with_emoji_fallback(lambda ce: c.bot.send_message(
        chat_id, build_post(d, settings, ce), disable_web_page_preview=True))


async def send_preview(d: dict, chat_id: int | None = None):
    """Предпросмотр в том виде, в каком пост выйдет: фото с текстом в подписи (без custom emoji)."""
    c = get_ctx()
    chat_id = chat_id or d["manager_chat_id"]
    photos = c.db.photos(d["id"])
    miss = missing_required(d["type"], d["fields"])
    settings = c.settings_for_post()
    over = caption_overflow(d, settings)

    def caption(custom_emoji: bool):
        return None if miss or over else build_post(d, settings, custom_emoji)

    if len(photos) == 1:  # альбом в Telegram — минимум 2 фото; показываем черновик как есть
        await with_emoji_fallback(lambda ce: c.bot.send_photo(chat_id, photos[0]["file_id"], caption=caption(ce)))
    elif photos:
        await with_emoji_fallback(lambda ce: c.bot.send_media_group(chat_id, album_media(photos, caption(ce))))
    if miss:
        await c.bot.send_message(chat_id, f"⚠️ Предпросмотр {d['code']} недоступен: не заполнено — "
                                 + ", ".join(esc(f.label) for f in miss))
    elif over or not photos:  # подписи не получилось: текст всё равно показываем, чтобы его можно было сократить
        await send_post_text(d, chat_id)
    if over:
        await c.bot.send_message(chat_id, f"⚠️ {d['code']}: {esc(overflow_text(over))}")
