"""Подтверждение: автор сам решает, публиковать пост сразу или запланировать. Согласования нет."""
import logging

import database as dbm
from handlers import schedule
from services.draft_context import get_ctx, user_label
from services.post_generator import esc
from services.telegram_publisher import publish_draft
from services.validation import validate_for_publish

log = logging.getLogger("mvauto")

# PENDING — заявки, отправленные на согласование до его отмены: их тоже можно подтвердить
CONFIRMABLE = (dbm.DRAFT, dbm.CHANGES, dbm.PENDING)


def _problems_alert(problems: list[str]) -> str:
    return ("⚠️ " + "\n".join(problems))[:190]


def _claim_approved(d: dict, user, **extra) -> bool:
    """Отметка «подтверждено» для текущей версии. False, если статус уже изменился (двойное нажатие)."""
    c = get_ctx()
    now = dbm.now_iso()
    if not c.db.claim(d["id"], CONFIRMABLE, dbm.APPROVED, approved_by=user.id, approved_by_name=user_label(user),
                      approved_at=now, approved_version=d["version"], **extra):
        return False
    c.db.q("INSERT INTO approvals(draft_id,version,decision,user_id,comment,ts) VALUES(?,?,?,?,?,?)",
           (d["id"], d["version"], "approved", user.id, "", now))
    c.db.audit(user.id, d["id"], "approved", f"v{d['version']}")
    return True


async def h_submit(cb, d, arg):
    """Подтверждение сразу публикует пост. Отчёт об успехе отправляет publish_draft."""
    c = get_ctx()
    async with c.lock(d["id"]):
        d = c.db.get_draft(d["id"])
        problems = validate_for_publish(d, c.db.photos(d["id"]), c.tz, c.settings_for_post())
        if problems:
            return await cb.answer(_problems_alert(problems), show_alert=True)
        if not _claim_approved(d, cb.from_user, submitted_at=dbm.now_iso(), submitted_version=d["version"]):
            return await cb.answer("🔄 Уже отправлено или статус изменился.", show_alert=True)
        c.db.deactivate_draft_prompts(d["id"])
    await cb.answer("🚀 Публикую…")
    ok, msg = await publish_draft(d["id"], cb.from_user.id)
    if not ok:
        await cb.message.answer(f"⚠️ {d['code']}: {esc(msg)}")


async def h_approve_schedule(cb, d, arg):
    """Отдельная кнопка «Запланировать»: подтверждает пост без немедленной публикации и спрашивает время."""
    c = get_ctx()
    async with c.lock(d["id"]):
        d = c.db.get_draft(d["id"])
        problems = validate_for_publish(d, c.db.photos(d["id"]), c.tz, c.settings_for_post())
        if problems:
            return await cb.answer(_problems_alert(problems), show_alert=True)
        if not _claim_approved(d, cb.from_user):
            return await cb.answer("🔄 Статус уже изменился.", show_alert=True)
        c.db.deactivate_draft_prompts(d["id"])
    await schedule.h_ask_schedule(cb, c.db.get_draft(d["id"]), arg)
