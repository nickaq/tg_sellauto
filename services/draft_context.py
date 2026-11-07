"""Контекст приложения, блокировки по черновикам, версионирование, аудит."""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

import database as dbm
from config import Config
from database import Database

log = logging.getLogger("mvauto")


@dataclass
class AppContext:
    bot: Any
    db: Database
    cfg: Config
    scheduler: Any = None
    locks: dict = field(default_factory=dict)

    def lock(self, draft_id: int) -> asyncio.Lock:
        return self.locks.setdefault(draft_id, asyncio.Lock())

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.db.get_setting("timezone", self.cfg.timezone))

    @property
    def managers_chat_id(self):
        return self.db.get_setting("managers_chat_id")

    def target_chat(self, type_: str):
        return self.db.get_setting("sales_chat_id" if type_ == "sales" else "auction_chat_id")

    def role(self, user_id: int):
        return self.db.get_role(user_id)

    def settings_for_post(self) -> dict:
        g = self.db.get_setting
        return {"links": g("links", {}), "custom_emoji": g("custom_emoji", {}),
                "power_suffix": g("power_suffix", "")}


_ctx: AppContext | None = None


def set_ctx(c: AppContext):
    global _ctx
    _ctx = c


def get_ctx() -> AppContext:
    assert _ctx is not None, "context is not initialised"
    return _ctx


def seed_from_config(db: Database, cfg: Config):
    """Первичные значения из .env (БД имеет приоритет, если уже задано)."""
    for key, val in (("managers_chat_id", cfg.managers_chat_id), ("sales_chat_id", cfg.sales_chat_id),
                     ("auction_chat_id", cfg.auction_chat_id), ("timezone", cfg.timezone)):
        if db.get_setting(key) is None and val is not None:
            db.set_setting(key, val)
    # Отозванный через /removeuser (active=0) не возвращается при перезапуске.
    for uid in cfg.admin_ids:
        if not db.user_exists(uid) or db.get_role(uid) is not None:
            db.set_user(uid, "admin", added_by=0)
    for uid in cfg.manager_ids:
        if not db.user_exists(uid):
            db.set_user(uid, "manager", added_by=0)


def user_label(user) -> str:
    return f"@{user.username}" if user.username else (user.full_name or str(user.id))


def mention(user_id: int, name: str | None = None) -> str:
    import html
    return f'<a href="tg://user?id={user_id}">{html.escape(name or str(user_id))}</a>'


def can_edit_draft(d: dict, user_id: int) -> bool:
    role = get_ctx().role(user_id)
    return role == "admin" or (role == "manager" and d["creator_id"] == user_id)


def find_prompt(m, kinds):
    """Ответ принадлежит активному вопросу этого чата (Reply на конкретный вопрос тоже работает)."""
    c = get_ctx()
    if m.reply_to_message:
        p = c.db.get_prompt(m.chat.id, m.reply_to_message.message_id)
        if p and p["kind"] in kinds:
            return p
    return c.db.latest_prompt(m.chat.id, kinds)


def touch(draft_id: int, user_id: int, **extra) -> dict:
    """Любое изменение данных: +версия; подтверждение/расписание сбрасываются (нужно подтвердить заново)."""
    c = get_ctx()
    d = c.db.get_draft(draft_id)
    upd = {"version": d["version"] + 1, "editor_id": user_id}
    if d["status"] in (dbm.PENDING, dbm.APPROVED, dbm.SCHEDULED, dbm.CHANGES):
        upd.update(status=dbm.DRAFT, approved_by=None, approved_by_name=None, approved_at=None,
                   approved_version=None, scheduled_at=None, submitted_version=None)
        if d["status"] == dbm.SCHEDULED and c.scheduler:
            c.scheduler.remove(draft_id)
        c.db.audit(user_id, draft_id, "reset_to_draft", f"было {d['status']}")
    upd.update(extra)
    c.db.update_draft(draft_id, **upd)
    return c.db.get_draft(draft_id)
