"""Фотографии черновика: приём, замена, удаление, завершение загрузки."""
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

import database as dbm
from handlers import ai_fill
from handlers.permissions import PrivateOnly, is_staff
from services import photo_manager
from services.cards import photo_prompt_kb, refresh_card, send_photo_prompt, send_preview, show_card, update_photo_prompt
from services.draft_context import find_prompt, get_ctx
from services.validation import photos_problem

router = Router()
router.message.filter(PrivateOnly())


@router.message(F.photo)
async def on_photo(m: Message):
    if not m.from_user or not is_staff(m.from_user.id):
        return
    p = find_prompt(m, ("photos", "ai"))
    if p:
        ai = get_ctx().db.latest_prompt(m.chat.id, ("ai",))
        await photo_manager.handle_photo(m, p)
        if m.caption and ai and ai["draft_id"] == p["draft_id"]:
            await ai_fill.on_ai_caption(m, ai)
    else:
        await m.reply("📷 Чтобы добавить фото, откройте черновик и нажмите «📷 Фото» (/menu → 📝 Мои черновики).")


@router.message(F.document.mime_type.startswith("image/"))
async def on_image_document(m: Message):
    if m.from_user and is_staff(m.from_user.id):
        await m.reply("📎 Это файл, а нужна фотография: отправьте изображение как фото, без опции «файлом».")


def _locked(d: dict) -> bool:
    """Кнопки старых сообщений остаются нажимаемыми, поэтому статус проверяется при каждом нажатии."""
    return d["status"] not in dbm.EDITABLE


async def h_ph(cb, d, arg):
    if _locked(d):
        return await cb.answer("🔒 Изменение недоступно.", show_alert=True)
    await cb.answer()
    await send_photo_prompt(d, cb.message.chat.id)


async def _photo_mode(cb, d, mode):
    if _locked(d):
        return await cb.answer("🔒 Изменение недоступно.", show_alert=True)
    if not get_ctx().db.photos(d["id"]):
        return await cb.answer("📭 Фото ещё нет.", show_alert=True)
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=photo_prompt_kb(d, mode))


async def h_phd(cb, d, arg):
    await _photo_mode(cb, d, "del")


async def h_phr(cb, d, arg):
    await _photo_mode(cb, d, "rep")


async def h_phb(cb, d, arg):
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=photo_prompt_kb(d))
    except TelegramBadRequest:
        pass


async def h_phdel(cb, d, arg):
    ok = await photo_manager.delete_photo(d["id"], int(arg), cb.from_user.id)
    await cb.answer("🗑 Удалено" if ok else "⚠️ Удаление недоступно", show_alert=not ok)
    await update_photo_prompt(d["id"])
    await refresh_card(d["id"])


async def h_phrep(cb, d, arg):
    c = get_ctx()
    if _locked(d):
        return await cb.answer("🔒 Изменение недоступно.", show_alert=True)
    async with c.lock(d["id"]):
        c.db.update_draft(d["id"], replace_pos=int(arg))
    await cb.answer(f"🔄 Пришлите новое фото — оно заменит №{arg}.", show_alert=True)
    await update_photo_prompt(d["id"])


async def h_phc(cb, d, arg):
    ok = await photo_manager.clear_photos(d["id"], cb.from_user.id)
    await cb.answer("🧹 Все фото удалены" if ok else "🔒 Изменение недоступно.", show_alert=not ok)
    await update_photo_prompt(d["id"])
    await refresh_card(d["id"])


async def h_phdone(cb, d, arg):
    """Завершение загрузки: предпросмотр и карточка с кнопками Подтвердить / Редактировать / Отменить."""
    c = get_ctx()
    problem = photos_problem(len(c.db.photos(d["id"])))
    if problem or _locked(d):
        return await cb.answer(f"⚠️ {problem}" if problem else "🔒 Изменение недоступно.", show_alert=True)
    await cb.answer("✅ Загрузка завершена")
    c.db.deactivate_prompt(cb.message.chat.id, cb.message.message_id)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await send_preview(d, cb.message.chat.id)
    await show_card(d, cb.message.chat.id)


HANDLERS = {"ph": h_ph, "phd": h_phd, "phr": h_phr, "phb": h_phb, "phdel": h_phdel, "phrep": h_phrep, "phc": h_phc,
            "phdone": h_phdone}
