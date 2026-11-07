"""История: все поля из раздела 11 ТЗ, фильтры по статусу, группе, периоду, автору и страницы."""
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup, Message

import database as dbm
from handlers.permissions import PrivateOnly, is_admin, is_staff
from services.cards import GROUP_RU, STATUS_RU, TYPE_RU, fmt_dt
from services.draft_context import get_ctx
from services.post_generator import esc

router = Router()
router.message.filter(PrivateOnly())
router.callback_query.filter(PrivateOnly())

TITLE = "📚 История публикаций"
PAGE_SIZE = 7  # каждая запись до ~450 символов: страница не превышает лимит Telegram в 4096
STATUS = {"a": (None, "Все"), "d": (dbm.DRAFT, "Черн."), "s": (dbm.SCHEDULED, "План"),
          "p": (dbm.PUBLISHED, "Опубл."), "c": (dbm.CANCELLED, "Отмен."), "f": (dbm.FAILED, "Ошибки"),
          "r": (dbm.REJECTED, "Отклон.")}
GROUP = {"a": (None, "Все группы"), "s": ("sales", "№1"), "u": ("auction", "№2")}
PERIOD = {"0": (None, "Всё время"), "7": (7, "7 дн."), "30": (30, "30 дн.")}


def build(st, gr, per, mine, uid, admin):
    where, params = ["1=1"], []
    if STATUS[st][0]:
        where.append("status=?")
        params.append(STATUS[st][0])
    if GROUP[gr][0]:
        where.append("type=?")
        params.append(GROUP[gr][0])
    if PERIOD[per][0]:
        since = datetime.now(timezone.utc) - timedelta(days=PERIOD[per][0])
        where.append("created_at>=?")
        params.append(since.isoformat(timespec="seconds"))
    if mine == "1" or not admin:  # менеджер видит только свою историю
        where.append("creator_id=?")
        params.append(uid)
    return " AND ".join(where), tuple(params)


def render_entry(d: dict, pub: dict) -> str:
    lines = [f"🆔 <b>{d['code']}</b> · {esc(d['fields'].get('model') or '—')}",
             f"   {TYPE_RU[d['type']]} → {GROUP_RU[d['type']]}",
             f"   📌 {STATUS_RU.get(d['status'], d['status'])} · 👤 {esc(d['creator_name'] or '—')}"
             f" · 👍 {esc(d['approved_by_name'] or '—')}",
             f"   🗓 Создано: {fmt_dt(d['created_at'])} · 🕒 План: {fmt_dt(d['scheduled_at'])}"
             f" · 📣 Факт: {fmt_dt(d['published_at'])}"]
    if pub.get("text_id"):
        lines.append(f"   💬 Message ID: {pub['text_id']}")
    if d.get("error") and d["status"] == dbm.FAILED:
        lines.append(f"   ⚠️ {esc(d['error'][:100])}")
    return "\n".join(lines)


@router.message(Command("history"))
async def cmd_history(m: Message):
    if m.from_user and is_staff(m.from_user.id):
        text, kb = history_view(m.from_user.id, "a", "a", "0", "0", 0)
        await m.answer(text, reply_markup=kb)


def history_view(uid, st, gr, per, mine, page) -> tuple[str, InlineKeyboardMarkup]:
    c = get_ctx()
    admin = is_admin(uid)
    where, params = build(st, gr, per, mine, uid, admin)
    found = c.db.list_drafts(where, params, limit=PAGE_SIZE + 1, offset=page * PAGE_SIZE)
    has_next = len(found) > PAGE_SIZE
    drafts = found[:PAGE_SIZE]
    lines = [f"{TITLE}\n🔎 {STATUS[st][1]} · {GROUP[gr][1]} · {PERIOD[per][1]}"
             + (" · мои" if mine == "1" or not admin else " · все авторы")
             + (f" · стр. {page + 1}" if page or has_next else "")]
    if not drafts:
        lines.append("📭 Ничего не найдено.")
    for d in drafts:
        lines.append(render_entry(d, c.db.get_publication(d["id"]) or {}))

    def row(table, idx, make):
        return [B(text=("• " if k == [st, gr, per][idx] else "") + v[1], callback_data=make(k))
                for k, v in table.items()]
    statuses = row(STATUS, 0, lambda k: f"m:h:{k}:{gr}:{per}:{mine}")
    rows = [statuses[:4], statuses[4:],
            row(GROUP, 1, lambda k: f"m:h:{st}:{k}:{per}:{mine}"),
            row(PERIOD, 2, lambda k: f"m:h:{st}:{gr}:{k}:{mine}")]
    nav = []
    if page:
        nav.append(B(text="⬅️ Новее", callback_data=f"m:h:{st}:{gr}:{per}:{mine}:{page - 1}"))
    if has_next:
        nav.append(B(text="Старее ➡️", callback_data=f"m:h:{st}:{gr}:{per}:{mine}:{page + 1}"))
    if nav:
        rows.append(nav)
    if admin:
        rows.append([B(text="👤 Только мои" if mine != "1" else "👥 Все авторы",
                       callback_data=f"m:h:{st}:{gr}:{per}:{'1' if mine != '1' else '0'}")])
    return "\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("m:h:"))
async def on_history(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_staff(uid):
        return await cb.answer("⛔ Нет доступа.", show_alert=True)
    parts = cb.data.split(":")
    st, gr, per, mine = parts[2:6]
    page = int(parts[6]) if len(parts) > 6 else 0
    text, kb = history_view(uid, st, gr, per, mine, page)
    await cb.answer()
    if (cb.message.text or "").startswith(TITLE):
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass  # фильтр не изменился
    else:  # открыто из главного меню — новое сообщение
        await cb.message.answer(text, reply_markup=kb)
