"""Заполнение черновика нейросетью: менеджер присылает текст (можно вместе с фото), бот разбирает его сам."""
import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup

import database as dbm
from services.ai_parser import AIError, parse_ad
from services.cards import ask_field, refresh_card, repost_photo_prompt, send_photo_prompt, send_post_text
from services.draft_context import can_edit_draft, get_ctx, touch
from services.post_generator import esc
from services.validation import get_form, missing_required, next_missing

log = logging.getLogger("mvauto")


def ai_enabled() -> bool:
    return bool(get_ctx().cfg.groq_api_key)


async def ask_ai_text(d: dict, chat_id: int, note: str = ""):
    """Первый запрос — текст объявления (фото можно сразу). Если данные уже есть — один вопрос о недостающих."""
    c = get_ctx()
    kb = InlineKeyboardMarkup(inline_keyboard=[[B(text="✍️ Заполнить вручную", callback_data=f"d:man:{d['id']}:0")]])
    missing = missing_required(d["type"], d["fields"])
    if d["fields"] and missing:
        text = (f"{note}🤖 <b>{d['code']}</b> · Не хватает данных: {esc(', '.join(f.label for f in missing))}.\n"
                "Пришлите их одним сообщением.")
    else:
        text = (f"{note}🤖 <b>{d['code']}</b> · Пришлите текст объявления или перешлите готовый пост с фото.\n"
                "Нейросеть сама заполнит поля, а вы проверите результат перед публикацией.")
    m = await c.bot.send_message(chat_id, text, reply_markup=kb)
    c.db.add_prompt(chat_id, m.message_id, d["id"], "ai", replace_kinds=("ai",))


def _source_text(d: dict, text: str) -> str:
    """Ответ на вопрос о недостающем имеет смысл только вместе с контекстом: «полный, 276» само по себе непонятно."""
    known = {f.label: d["fields"][f.key] for f in get_form(d["type"]) if d["fields"].get(f.key)}
    if not known:
        return text
    missing = ", ".join(f.label for f in missing_required(d["type"], d["fields"]))
    facts = "; ".join(f"{k}: {v}" for k, v in known.items())
    return f"Уже известно: {facts}\nНе хватало: {missing}\nОтвет менеджера:\n{text}"


async def on_ai_text(m, p: dict, text: str):
    """Разбор текста. Вопрос закрывается сразу, чтобы второе сообщение не запустило разбор повторно."""
    c = get_ctx()
    uid = m.from_user.id
    d = c.db.get_draft(p["draft_id"])
    if not d or not can_edit_draft(d, uid):
        return
    c.db.deactivate_prompt(p["chat_id"], p["message_id"])
    if d["status"] not in dbm.EDITABLE:
        return await m.reply(f"🔒 {d['code']}: изменение недоступно в этом статусе.")
    if c.cfg.hide_ai:  # вопрос был задан до того, как ИИ скрыли
        return await m.reply(f"⏸ {d['code']}: ИИ временно недоступен. Заполните поля вручную: «✏️ Редактировать» "
                             "в карточке, список черновиков — /drafts.")
    await c.bot.send_chat_action(m.chat.id, "typing")
    try:
        fields, invalid = await parse_ad(c.cfg.groq_api_key, c.cfg.groq_model, d["type"], _source_text(d, text), c.tz)
        if not any(fields.values()):
            raise AIError("в тексте не найдено данных об автомобиле")
    except AIError as ex:
        c.db.audit(uid, d["id"], "ai_failed", str(ex))
        return await ask_ai_text(d, m.chat.id, f"⚠️ Не получилось: {esc(ex)}.\n\n")

    async with c.lock(d["id"]):
        d = c.db.get_draft(d["id"])
        if d["status"] not in dbm.EDITABLE:  # черновик отменили, пока ждали ответ нейросети
            return await m.reply(f"🔒 {d['code']}: изменение недоступно в этом статусе.")
        # То, что уже заполнено, важнее нового ответа нейросети; пустые необязательные поля можно дополнить.
        d = touch(d["id"], uid, fields={**fields, **{k: v for k, v in d["fields"].items() if v}})
        c.db.audit(uid, d["id"], "ai_fill", f"поля: {', '.join(k for k, v in fields.items() if v)}")

    await after_ai(d, invalid, m.chat.id)


async def after_ai(d: dict, invalid: list[str], chat_id: int):
    c = get_ctx()
    if missing_required(d["type"], d["fields"]):
        note = f"⚠️ Нейросеть вернула неверные значения, они не сохранены: {esc(', '.join(invalid))}.\n\n" if invalid else ""
        await refresh_card(d["id"], new=True)
        return await ask_ai_text(d, chat_id, note)
    if invalid:
        await c.bot.send_message(chat_id, "⚠️ Нейросеть вернула неверные значения, они не сохранены: "
                                 + esc(", ".join(invalid)))
    await c.bot.send_message(chat_id, "🤖 Проверьте, что получилось:")
    await send_post_text(d, chat_id)
    if c.db.active_prompt(d["id"], "photos"):
        await repost_photo_prompt(d["id"])  # фото уже приходят: вопрос переносим вниз диалога
    else:
        await send_photo_prompt(d, chat_id)
    await refresh_card(d["id"], new=True)


async def on_ai_caption(m, p: dict):
    """Альбом с подписью: сначала включаем приём фото, потом закрываем вопрос, чтобы фото альбома не потерялись."""
    c = get_ctx()
    d = c.db.get_draft(p["draft_id"])
    if d and can_edit_draft(d, m.from_user.id) and not c.db.active_prompt(d["id"], "photos"):
        await send_photo_prompt(d, m.chat.id)
    await on_ai_text(m, p, m.caption)


async def h_manual(cb, d, arg):
    """Заполнить вручную: обычные вопросы по одному полю."""
    c = get_ctx()
    p = c.db.get_prompt(cb.message.chat.id, cb.message.message_id)
    if not p or p["kind"] != "ai" or p["draft_id"] != d["id"]:
        return await cb.answer("⌛ Вопрос устарел.", show_alert=True)
    await cb.answer()
    c.db.deactivate_prompt(cb.message.chat.id, cb.message.message_id)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await ask_field(d, next_missing(d["type"], d["fields"]) or get_form(d["type"])[0], cb.message.chat.id)
