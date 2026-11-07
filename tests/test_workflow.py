import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import database as dbm
from services.draft_context import seed_from_config, touch
from services.telegram_publisher import publish_draft
from tests.conftest import fill_sales


def make_approved(ctx):
    d = ctx.db.create_draft("sales", 2, "@m", -1001)
    fill_sales(ctx, d["id"])
    d = ctx.db.get_draft(d["id"])
    ctx.db.update_draft(d["id"], status=dbm.APPROVED, approved_by=1, approved_by_name="@admin",
                        approved_version=d["version"])
    return d["id"]


async def test_roles(app):
    assert app.role(1) == "admin" and app.role(2) == "manager" and app.role(99) is None
    app.db.remove_user(2)
    assert app.role(2) is None


async def test_publish_is_one_album_with_caption_in_right_group(app):
    i = make_approved(app)
    ok, _ = await publish_draft(i, 1)
    assert ok
    assert [k for k, c, _ in app.bot.sent if c == "@sales"] == ["forward"]  # один пост, отдельного текста нет
    caption, *rest = app.bot.captions[0]
    assert "Цена в Грузии" in caption and rest == [None] * 9  # текст только у первого фото: он виден под альбомом
    assert not [s for s in app.bot.sent if s[0] == "msg" and "не применились" in s[2]]
    pub = app.db.get_publication(i)
    assert pub["text_id"] == pub["album_ids"][0]
    assert app.db.get_draft(i)["status"] == dbm.PUBLISHED
    assert not [s for s in app.bot.sent if s[1] == "@auction"]


async def test_double_click_publishes_once(app):
    i = make_approved(app)
    r = await asyncio.gather(publish_draft(i, 1), publish_draft(i, 1))
    assert sorted(x[0] for x in r) == [False, True]
    assert len([s for s in app.bot.sent if s[0] == "forward"]) == 1


async def test_unapproved_or_changed_after_approval_cannot_publish(app):
    d = app.db.create_draft("sales", 2, "@m", -1001)
    fill_sales(app, d["id"])
    assert not (await publish_draft(d["id"], 2))[0]  # менеджер без апрува
    i = make_approved(app)
    touch(i, 2)  # правка после одобрения → снова Draft, апрув сброшен
    assert app.db.get_draft(i)["status"] == dbm.DRAFT
    assert not (await publish_draft(i, 1))[0]


async def test_retry_after_interrupted_publish_does_not_resend(app):
    i = make_approved(app)
    assert (await publish_draft(i, 1))[0]
    app.db.update_draft(i, status=dbm.FAILED)  # сбой уже после отправки
    assert (await publish_draft(i, 1))[0]
    assert len([s for s in app.bot.sent if s[0] == "forward"]) == 1


async def test_too_long_post_is_not_published(app):
    i = make_approved(app)
    d = app.db.get_draft(i)
    app.db.update_draft(i, fields={**d["fields"], "subtitle": "x" * 1000})
    ok, msg = await publish_draft(i, 1)
    assert not ok and "длиннее лимита" in msg and not app.bot.sent


@pytest.mark.parametrize("count, allowed", [(0, False), (1, False), (2, False), (9, False), (10, True), (11, False)])
async def test_only_exactly_ten_photos_can_be_published(app, count, allowed):
    d = app.db.create_draft("sales", 2, "@m", -1001)
    fill_sales(app, d["id"], photos=count)
    app.db.update_draft(d["id"], status=dbm.APPROVED, approved_by=1, approved_by_name="a",
                        approved_version=app.db.get_draft(d["id"])["version"])
    ok, msg = await publish_draft(d["id"], 1)
    assert ok is allowed
    albums = [s for s in app.bot.sent if s[0] == "album"]
    if allowed:
        assert albums[0][2] == 10
    else:
        assert "фото" in msg and not albums


async def test_flood_control_retry_is_limited_and_does_not_duplicate(app, monkeypatch):
    from aiogram.exceptions import TelegramRetryAfter
    import services.telegram_publisher as tp

    async def no_sleep(_):
        return None
    monkeypatch.setattr(tp.asyncio, "sleep", no_sleep)
    calls = 0
    real = app.bot.send_media_group

    async def flaky(chat_id, media, **kw):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TelegramRetryAfter(method=SimpleNamespace(), message="flood", retry_after=1)
        return await real(chat_id, media, **kw)
    app.bot.send_media_group = flaky
    assert (await publish_draft(make_approved(app), 1))[0]
    assert calls == 3 and len([s for s in app.bot.sent if s[0] == "album"]) == 1

    calls = -100  # дальше Telegram отвечает отказом всегда: не больше 3 попыток, затем Failed
    i = make_approved(app)
    before = calls
    ok, _ = await publish_draft(i, 1)
    # по 3 попытки: сборка в личке и резервная прямая отправка
    assert not ok and calls - before == 6 and app.db.get_draft(i)["status"] == dbm.FAILED


async def test_schedule_overdue_more_than_week_becomes_failed_and_notifies(app):
    i = make_approved(app)
    run_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")
    app.db.update_draft(i, status=dbm.SCHEDULED, scheduled_at=run_at)
    app.scheduler.start()
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert app.db.get_draft(i)["status"] == dbm.FAILED and app.scheduler.next_run(i) is None
        assert any(s[0] == "msg" and s[1] == 1 and "недели" in s[2] for s in app.bot.sent)
    finally:
        app.scheduler.shutdown()


async def test_removed_user_stays_removed_after_restart(app):
    app.db.remove_user(2)
    seed_from_config(app.db, app.cfg)
    assert app.role(2) is None


async def test_new_prompt_closes_prompts_of_other_drafts_in_chat(app):
    a = app.db.create_draft("sales", 1, "@a", 1)
    b = app.db.create_draft("sales", 1, "@a", 1)
    other_chat = app.db.create_draft("sales", 2, "@m", 2)
    app.db.add_prompt(1, 10, a["id"], "field", "color")
    app.db.add_prompt(2, 11, other_chat["id"], "field", "color")
    app.db.add_prompt(1, 12, b["id"], "photos")
    assert app.db.latest_prompt(1, ("field",)) is None  # текст не уйдёт в старый черновик
    assert app.db.latest_prompt(2, ("field",))["draft_id"] == other_chat["id"]


async def test_scheduler_restore_after_restart(app):
    i = make_approved(app)
    run_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
    app.db.update_draft(i, status=dbm.SCHEDULED, scheduled_at=run_at)
    app.scheduler.start()  # restore() читает SQLite
    try:
        assert app.scheduler.next_run(i) is not None
    finally:
        app.scheduler.shutdown()


async def test_interrupted_publishing_becomes_failed_on_restart(app):
    i = make_approved(app)
    app.db.update_draft(i, status=dbm.PUBLISHING)
    app.scheduler.start()
    try:
        assert app.db.get_draft(i)["status"] == dbm.FAILED
    finally:
        app.scheduler.shutdown()


async def test_publish_keeps_quiet_when_emoji_applied(app):
    real = app.bot.send_media_group

    async def with_entities(chat_id, media, **kw):
        msgs = await real(chat_id, media, **kw)
        msgs[0].caption_entities = [SimpleNamespace(type="custom_emoji")]
        return msgs
    app.bot.send_media_group = with_entities
    assert (await publish_draft(make_approved(app), 1))[0]
    assert "tg-emoji" in app.bot.captions[0][0]
    assert not any(s[0] == "msg" and "не применились" in s[2] for s in app.bot.sent)
    kinds = [s[0] for s in app.bot.sent if s[0] in ("album", "forward", "delete")]
    assert kinds == ["album", "forward", "delete"]  # собран в личке, переслан, исходник удалён
    assert [s[1] for s in app.bot.sent if s[0] in ("album", "forward", "delete")] == [1, "@sales", 1]
