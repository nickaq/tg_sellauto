"""Личный диалог с ботом: ответы на вопросы формы, карточки и кнопки черновиков."""
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup, Message

import database as dbm
from handlers import ai_fill, approvals, photos, schedule
from handlers.permissions import PrivateOnly, is_staff
from services.cards import (FIELD_ICONS, ask_field, card_keyboard, refresh_card, render_card, send_card,
                            send_photo_prompt, send_preview, show_card)
from services.draft_context import can_edit_draft, find_prompt, get_ctx, touch
from services.validation import PHOTOS_REQUIRED, get_form, next_missing

log = logging.getLogger("mvauto")
router = Router()
router.message.filter(PrivateOnly())
router.callback_query.filter(PrivateOnly())

PUBLISH_ACTS = {"sub", "aps", "now", "sch", "uns", "retry", "fix"}
# Менеджер распоряжается только своими черновиками, администратор — любыми (can_edit_draft)
CREATOR_ACTS = PUBLISH_ACTS | {"edit", "fld", "ph", "phd", "phr", "phc", "phdel", "phrep", "phb", "phdone", "pv",
                               "cancel", "back", "open", "sk", "man"}
VERSIONED = {"now", "sch", "sub", "aps"}
# После этих действий карточка в чате нажавшего обновляется сразу
SYNC_ACTS = PUBLISH_ACTS | {"cancel"}


# ---------------------------------------------------------------- ввод полей
async def save_field(draft_id: int, key: str, raw: str | None, uid: int):
    """raw=None — пропуск/очистка необязательного поля. Возвращает (ошибка|None, черновик, был_полным)."""
    c = get_ctx()
    async with c.lock(draft_id):
        d = c.db.get_draft(draft_id)
        if d["status"] not in dbm.EDITABLE:
            return "Изменение недоступно в этом статусе.", d, True
        f = next(x for x in get_form(d["type"]) if x.key == key)
        if raw is None and f.required:
            return "Это поле обязательное.", d, True
        try:
            value = "" if raw is None else f.normalize(raw, c.tz)
        except ValueError as ex:
            return str(ex), d, True
        was_complete = next_missing(d["type"], d["fields"]) is None
        fields = dict(d["fields"])
        fields[key] = value
        d = touch(draft_id, uid, fields=fields)
        c.db.audit(uid, draft_id, "field", f"{key}={value}")
    return None, d, was_complete


async def after_field(d: dict, was_complete: bool, chat_id: int):
    c = get_ctx()
    nxt = next_missing(d["type"], d["fields"])
    if nxt:
        await ask_field(d, nxt, chat_id)
    elif not was_complete and len(c.db.photos(d["id"])) < PHOTOS_REQUIRED:
        await send_photo_prompt(d, chat_id)
    await refresh_card(d["id"], new=nxt is None)  # форма заполнена — карточка внизу диалога
    if not nxt and chat_id != d["manager_chat_id"]:  # черновик правит администратор, а не автор
        await send_card(c.db.get_draft(d["id"]), chat_id)


@router.message(F.text, ~F.text.startswith("/"))
async def on_text(m: Message):
    if not m.from_user or not is_staff(m.from_user.id):
        return
    c = get_ctx()
    p = find_prompt(m, ("field", "schedule", "ai"))
    if not p:
        return await m.answer("📋 Откройте меню: /menu")
    d = c.db.get_draft(p["draft_id"])
    uid = m.from_user.id
    if not d:
        return
    kind = p["kind"]
    if kind == "ai":
        await ai_fill.on_ai_text(m, p, m.text)
    elif kind == "field":
        if not can_edit_draft(d, uid):
            return
        err, d, was_complete = await save_field(d["id"], p["arg"], m.text, uid)
        if err:
            if d["status"] not in dbm.EDITABLE:  # вопрос больше не актуален — не просим отвечать снова
                c.db.deactivate_prompt(m.chat.id, p["message_id"])
                return await m.reply(f"🔒 {d['code']}: {err}")
            return await m.reply(f"⚠️ {err}\n✍️ Напишите ответ ещё раз.")
        c.db.deactivate_prompt(m.chat.id, p["message_id"])
        await after_field(d, was_complete, m.chat.id)
    elif kind == "schedule":
        if can_edit_draft(d, uid):
            await schedule.finish_schedule(m, p, d)


# ---------------------------------------------------------------- кнопки
async def deny(cb: CallbackQuery, text="⛔ Нет доступа."):
    await cb.answer(text, show_alert=True)


@router.callback_query(F.data.startswith("d:"))
async def on_draft_cb(cb: CallbackQuery):
    c = get_ctx()
    parts = cb.data.split(":")
    act, did, ver = parts[1], int(parts[2]), int(parts[3])
    arg = parts[4] if len(parts) > 4 else None
    uid = cb.from_user.id
    if not is_staff(uid):
        return await deny(cb)
    d = c.db.get_draft(did)
    if not d:
        return await cb.answer("🔍 Черновик не найден.", show_alert=True)
    if act in CREATOR_ACTS and not can_edit_draft(d, uid):
        return await deny(cb, "⛔ Это чужой черновик.")
    if act in {"sub", "aps", "now", "sch", "retry"} and d["type"] == "auction" and c.cfg.hide_auction:
        return await deny(cb, "⏸ Публикация аукционов временно недоступна. Черновик сохранён.")
    if act in VERSIONED and ver != d["version"]:
        await cb.answer("🔄 Карточка устарела — данные изменились. Обновляю.", show_alert=True)
        return await sync_admin_message(cb, did)

    h = HANDLERS.get(act)
    if not h:
        return await cb.answer()
    try:
        await h(cb, d, arg)
        if act in SYNC_ACTS:
            await sync_admin_message(cb, did)
    except TelegramBadRequest as ex:
        log.warning("callback %s failed: %s", cb.data, ex)
    except Exception:
        log.exception("callback %s failed", cb.data)
        try:
            await cb.answer("⚠️ Ошибка. Подробности в логе.", show_alert=True)
        except Exception:
            pass


async def sync_admin_message(cb: CallbackQuery, draft_id: int):
    """Карточка в чате администратора (не автора) обновляется на месте."""
    c = get_ctx()
    d = c.db.get_draft(draft_id)
    if cb.message.chat.id == d["manager_chat_id"]:
        return await refresh_card(draft_id)
    try:
        await cb.message.edit_text(render_card(d), reply_markup=card_keyboard(d))
    except TelegramBadRequest:
        pass


async def h_open(cb, d, arg):
    await cb.answer()
    await show_card(d, cb.message.chat.id)


async def h_back(cb, d, arg):
    await cb.answer()
    await sync_admin_message(cb, d["id"])


async def h_edit(cb, d, arg):
    if d["status"] not in dbm.EDITABLE:
        return await cb.answer("🔒 Изменение недоступно.", show_alert=True)
    rows, row = [], []
    for f in get_form(d["type"]):
        mark = "✓ " if d["fields"].get(f.key) else ""
        row.append(B(text=f"{mark}{FIELD_ICONS.get(f.key, '')} {f.label}", callback_data=f"d:fld:{d['id']}:0:{f.key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([B(text="⬅️ Назад", callback_data=f"d:back:{d['id']}:0")])
    note = None
    if d["status"] in (dbm.APPROVED, dbm.SCHEDULED, dbm.PENDING):
        note = "⚠️ После изменения публикацию нужно будет подтвердить заново, а её расписание будет снято."
    await cb.answer(note, show_alert=bool(note))
    await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def h_fld(cb, d, arg):
    f = next((x for x in get_form(d["type"]) if x.key == arg), None)
    if not f or d["status"] not in dbm.EDITABLE:
        return await cb.answer("🔒 Недоступно.", show_alert=True)
    await cb.answer()
    await ask_field(d, f, cb.message.chat.id)
    await sync_admin_message(cb, d["id"])


async def h_skip(cb, d, arg):
    c = get_ctx()
    p = c.db.get_prompt(cb.message.chat.id, cb.message.message_id)
    if not p or p["kind"] != "field" or p["draft_id"] != d["id"]:
        return await cb.answer("⌛ Вопрос устарел.", show_alert=True)
    err, d, was_complete = await save_field(d["id"], p["arg"], None, cb.from_user.id)
    if err:
        return await cb.answer(f"⚠️ {err}", show_alert=True)
    await cb.answer("⏭ Пропущено")
    c.db.deactivate_prompt(cb.message.chat.id, cb.message.message_id)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await after_field(d, was_complete, cb.message.chat.id)


async def h_pv(cb, d, arg):
    await cb.answer()
    await send_preview(d, cb.message.chat.id)  # туда, где нажали, а не автору черновика
    await show_card(d, cb.message.chat.id)


async def h_cancel(cb, d, arg):
    c = get_ctx()
    async with c.lock(d["id"]):
        if not c.db.claim(d["id"], (dbm.DRAFT, dbm.CHANGES, dbm.PENDING, dbm.APPROVED, dbm.SCHEDULED, dbm.FAILED),
                          dbm.CANCELLED, scheduled_at=None):
            return await cb.answer("🔒 Нельзя отменить в этом статусе.", show_alert=True)
        if c.scheduler:
            c.scheduler.remove(d["id"])
        c.db.deactivate_draft_prompts(d["id"])
        c.db.audit(cb.from_user.id, d["id"], "cancel")
    pub = c.db.get_publication(d["id"])
    if pub and pub["album_ids"]:
        await cb.answer("🚫 Отменено. Альбом уже в группе — при необходимости удалите его вручную.", show_alert=True)
    else:
        await cb.answer("🚫 Отменено")


HANDLERS = {
    "open": h_open, "back": h_back, "edit": h_edit, "fld": h_fld, "sk": h_skip, "pv": h_pv, "cancel": h_cancel,
    "man": ai_fill.h_manual,
    **photos.HANDLERS,
    "sub": approvals.h_submit, "aps": approvals.h_approve_schedule,
    "now": schedule.h_now, "sch": schedule.h_ask_schedule, "uns": schedule.h_unschedule,
    "retry": schedule.h_retry, "fix": schedule.h_reopen,
}
