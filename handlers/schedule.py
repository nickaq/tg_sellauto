"""Публикация сейчас, планирование, перенос, отмена, повтор."""
import logging
from datetime import datetime, timezone

import database as dbm
from services.cards import fmt_dt, refresh_card, send_card
from services.draft_context import get_ctx
from services.post_generator import esc
from services.telegram_publisher import publish_draft
from services.validation import parse_schedule, validate_for_publish

log = logging.getLogger("mvauto")


async def h_now(cb, d, arg):
    if d["status"] != dbm.APPROVED:
        return await cb.answer("🔒 Публикация доступна только для подтверждённой заявки.", show_alert=True)
    await cb.answer("🚀 Публикую…")
    ok, msg = await publish_draft(d["id"], cb.from_user.id)
    if not ok:
        await cb.message.answer(f"⚠️ {d['code']}: {esc(msg)}")


async def h_retry(cb, d, arg):
    if d["status"] != dbm.FAILED:
        return await cb.answer("🔄 Повтор не требуется: статус уже изменился.", show_alert=True)
    await cb.answer("🔁 Повторяю…")
    ok, msg = await publish_draft(d["id"], cb.from_user.id)
    if not ok:
        await cb.message.answer(f"⚠️ {d['code']}: {esc(msg)}")


async def h_reopen(cb, d, arg):
    """Failed → Draft, чтобы исправить данные. Только если в группу ещё ничего не ушло."""
    c = get_ctx()
    async with c.lock(d["id"]):
        pub = c.db.get_publication(d["id"])
        if pub and pub["album_ids"]:
            return await cb.answer("🔒 Альбом уже в группе: используйте «Повторить» или «Отменить».", show_alert=True)
        if not c.db.claim(d["id"], (dbm.FAILED,), dbm.DRAFT, approved_by=None, approved_by_name=None,
                          approved_at=None, approved_version=None, scheduled_at=None, submitted_version=None,
                          error=None):
            return await cb.answer("🔄 Статус уже изменился.", show_alert=True)
        c.db.audit(cb.from_user.id, d["id"], "reopen")
    await cb.answer("✏️ Возвращено в черновик")


async def h_ask_schedule(cb, d, arg):
    c = get_ctx()
    if d["status"] not in (dbm.APPROVED, dbm.SCHEDULED):
        return await cb.answer("🔒 Сначала подтвердите заявку.", show_alert=True)
    await cb.answer()
    m = await c.bot.send_message(
        cb.message.chat.id,
        f"🕒 <b>{d['code']}</b>: напишите дату и время публикации сообщением.\n"
        f"📅 Формат: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>, часовой пояс: <b>{c.tz.key}</b>.")
    c.db.add_prompt(cb.message.chat.id, m.message_id, d["id"], "schedule", str(d["version"]),
                    replace_kinds=("schedule",))


def _schedule_problem(d: dict, dt: datetime, tz) -> str | None:
    problems = validate_for_publish(d, get_ctx().db.photos(d["id"]), tz, get_ctx().settings_for_post())
    if problems:
        return "; ".join(problems)
    date = d["fields"].get("auction_date")
    if d["type"] == "auction" and date and dt.date() > datetime.strptime(date, "%d.%m.%Y").date():
        return f"Публикация позже даты торгов ({date}). Выберите более раннее время."
    return None


async def finish_schedule(m, p, d):
    c = get_ctx()
    try:
        dt = parse_schedule(m.text, c.tz)
    except ValueError as ex:
        return await m.reply(f"⚠️ {esc(ex)}")
    async with c.lock(d["id"]):
        d = c.db.get_draft(d["id"])
        if str(d["version"]) != p["arg"] or d["status"] not in (dbm.APPROVED, dbm.SCHEDULED):
            c.db.deactivate_prompt(m.chat.id, p["message_id"])
            return await m.reply("🔄 Заявка изменилась — запланируйте заново.")
        problem = _schedule_problem(d, dt, c.tz)
        if problem:
            return await m.reply(f"⚠️ {esc(problem)}")
        iso = dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        c.db.claim(d["id"], (dbm.APPROVED, dbm.SCHEDULED), dbm.SCHEDULED, scheduled_at=iso)
        c.scheduler.add(d["id"], iso)
        c.db.audit(m.from_user.id, d["id"], "scheduled", f"{iso}")
        c.db.deactivate_prompt(m.chat.id, p["message_id"])
    await m.reply(f"🕒 {d['code']} запланирован на {fmt_dt(iso)} ({c.tz.key}).")
    await refresh_card(d["id"])
    if m.chat.id != d["manager_chat_id"]:
        await send_card(c.db.get_draft(d["id"]), m.chat.id)


async def h_unschedule(cb, d, arg):
    c = get_ctx()
    async with c.lock(d["id"]):
        if not c.db.claim(d["id"], (dbm.SCHEDULED,), dbm.APPROVED, scheduled_at=None):
            return await cb.answer("🔄 Не запланировано.", show_alert=True)
        c.scheduler.remove(d["id"])
        c.db.audit(cb.from_user.id, d["id"], "unscheduled")
    await cb.answer("⏸ Снято с расписания, публикация остаётся подтверждённой")
