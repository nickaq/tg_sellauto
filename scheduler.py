"""APScheduler + восстановление заданий из SQLite после перезапуска."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

import database as dbm
from services.draft_context import get_ctx
from services.post_generator import esc

log = logging.getLogger("mvauto")
MISFIRE_GRACE = timedelta(days=7)  # пропущенное при выключенном компьютере публикуем не позже этого срока


async def _run(draft_id: int, scheduled_at: str):
    from services.telegram_publisher import notify_participants, publish_draft
    c = get_ctx()
    d = c.db.get_draft(draft_id)
    if not d or d["status"] != dbm.SCHEDULED or d["scheduled_at"] != scheduled_at:
        return  # перенесено/отменено
    ok, msg = await publish_draft(draft_id, None)
    log.info("scheduled publish %s: %s %s", draft_id, ok, msg)
    if not ok:
        d = c.db.get_draft(draft_id)
        if d and d["status"] == dbm.SCHEDULED:  # отказ до захвата (например, проверка не пройдена)
            c.db.update_draft(draft_id, status=dbm.FAILED, error=msg)
            from services.cards import refresh_card
            await notify_participants(d, f"⚠️ {d['code']}: плановая публикация не выполнена. {esc(msg)}")
            await refresh_card(draft_id)


async def _notify(notices: list[tuple[dict, str]]):
    from services.telegram_publisher import notify_participants
    for d, text in notices:
        await notify_participants(d, text)


class PublishScheduler:
    def __init__(self):
        self.sched = AsyncIOScheduler(timezone="UTC")

    def start(self):
        self.sched.start()
        self.restore()

    def shutdown(self):
        self.sched.shutdown(wait=False)

    def add(self, draft_id: int, run_at_iso: str):
        run_at = datetime.fromisoformat(run_at_iso)
        self.sched.add_job(_run, DateTrigger(run_date=run_at), args=[draft_id, run_at_iso],
                           id=f"pub_{draft_id}", replace_existing=True,
                           misfire_grace_time=int(MISFIRE_GRACE.total_seconds()), coalesce=True)

    def remove(self, draft_id: int):
        try:
            self.sched.remove_job(f"pub_{draft_id}")
        except Exception:
            pass

    def restore(self):
        c = get_ctx()
        notices = []
        # прерванные публикации не повторяем вслепую — решение принимает автор через «Повторить»
        for d in c.db.list_drafts("status=?", (dbm.PUBLISHING,), limit=100):
            pub = c.db.get_publication(d["id"])
            if pub and pub["album_ids"]:
                error = "Публикация прервана перезапуском бота. Повтор отправит только текст."
            else:
                error = "Публикация прервана перезапуском бота. Перед повтором проверьте группу: альбом мог уйти."
            c.db.update_draft(d["id"], status=dbm.FAILED, error=error)
            notices.append((d, f"⚠️ <b>{d['code']}</b>: {error}"))
        n = 0
        oldest = datetime.now(timezone.utc) - MISFIRE_GRACE
        for d in c.db.list_drafts("status=?", (dbm.SCHEDULED,), limit=1000):
            if not d["scheduled_at"]:
                continue
            if datetime.fromisoformat(d["scheduled_at"]) < oldest:  # иначе APScheduler молча потеряет задание
                error = "Плановое время прошло больше недели назад, публикация не отправлена автоматически."
                c.db.update_draft(d["id"], status=dbm.FAILED, error=error)
                notices.append((d, f"⚠️ <b>{d['code']}</b>: {error}"))
                continue
            self.add(d["id"], d["scheduled_at"])
            n += 1
        log.info("scheduler restored: %s jobs", n)
        if notices:
            asyncio.get_running_loop().create_task(_notify(notices))

    def next_run(self, draft_id: int):
        j = self.sched.get_job(f"pub_{draft_id}")
        return j.next_run_time if j else None
