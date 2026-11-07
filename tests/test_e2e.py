"""Сквозные сценарии: настоящий Dispatcher и все роутеры, вместо сети — подставная сессия.
В Telegram ничего не отправляется. Каждое сообщение бота проверяется на корректный HTML и лимит длины."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from html.parser import HTMLParser

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import (AnswerCallbackQuery, DeleteMessages, EditMessageReplyMarkup, EditMessageText,
                             ForwardMessages, SendMediaGroup, SendMessage, SendPhoto)
from aiogram.types import CallbackQuery, Chat, Document, Message, MessageEntity, MessageId, PhotoSize, Update, User

import database as dbm
from bot import build_dispatcher
from config import Config
from scheduler import PublishScheduler
from services import photo_manager
from services.draft_context import AppContext, seed_from_config, set_ctx
from services.post_generator import CAPTION_LIMIT, visible_length

ADMIN, MANAGER, STRANGER = 1, 2, 99
ALLOWED_TAGS = {"b", "i", "a", "code", "tg-emoji", "blockquote"}


class TelegramHtml(HTMLParser):
    """Отвергает то, что отверг бы Telegram: неизвестные теги и незакрытую разметку."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []

    def handle_starttag(self, tag, attrs):
        assert tag in ALLOWED_TAGS, f"тег <{tag}> не поддерживается"
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag, f"лишний </{tag}>"


def check_html(text: str):
    assert len(text) <= 4096, f"сообщение длиной {len(text)}"
    parser = TelegramHtml()
    parser.feed(text)
    parser.close()
    assert not parser.stack, f"не закрыты теги: {parser.stack}"


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.last_ids = {}
        self.strip_emoji = False  # True: Telegram молча убирает custom emoji даже в личке
        self._id = 1000

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):
        yield b""

    def _msg(self, chat_id):
        self._id += 1
        cid = chat_id if isinstance(chat_id, int) else -100500
        return Message(message_id=self._id, date=datetime.now(timezone.utc),
                       chat=Chat(id=cid, type="private" if cid > 0 else "supergroup"))

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, (SendMessage, EditMessageText)):
            check_html(method.text)
        for caption in (getattr(m, "caption", None) for m in getattr(method, "media", [])):
            if caption:
                check_html(caption)
                assert visible_length(caption) <= CAPTION_LIMIT
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendPhoto)):
            result = self._msg(method.chat_id)
            self.last_ids[id(method)] = result.message_id
            return result
        if isinstance(method, SendMediaGroup):
            msgs = [self._msg(method.chat_id).model_copy(update={"media_group_id": "g"}) for _ in method.media]
            # Как настоящий Telegram: в личке custom emoji применяются, в каналах бот их теряет.
            if isinstance(method.chat_id, int) and method.chat_id > 0 and not self.strip_emoji \
                    and "tg-emoji" in (method.media[0].caption or ""):
                msgs[0] = msgs[0].model_copy(update={"caption_entities": [
                    MessageEntity(type="custom_emoji", offset=0, length=1, custom_emoji_id="1")]})
            return msgs
        if isinstance(method, ForwardMessages):
            return [MessageId(message_id=self._msg(method.chat_id).message_id) for _ in method.message_ids]
        if isinstance(method, EditMessageText):
            return self._msg(method.chat_id)
        return True


class Harness:
    def __init__(self, ctx, dp, bot, session):
        self.ctx, self.dp, self.bot, self.session = ctx, dp, bot, session
        self.update_id = 0
        self.incoming_id = 500000

    def _next_incoming(self):
        self.incoming_id += 1
        return self.incoming_id

    async def _feed(self, **kw):
        self.update_id += 1
        await self.dp.feed_update(self.bot, Update(update_id=self.update_id, **kw))

    def _message(self, uid, chat_id, chat_type="private", **kw):
        user = User(id=uid, is_bot=False, first_name=f"u{uid}", username=f"u{uid}")
        return Message(message_id=self._next_incoming(), date=datetime.now(timezone.utc),
                       chat=Chat(id=chat_id, type=chat_type), from_user=user, **kw)

    async def text(self, uid, text, chat_id=None, chat_type="private"):
        await self._feed(message=self._message(uid, chat_id or uid, chat_type, text=text))

    async def photo(self, uid, n, caption=None):
        ph = PhotoSize(file_id=f"file{n}", file_unique_id=f"uniq{n}", width=10, height=10)
        await self._feed(message=self._message(uid, uid, photo=[ph], caption=caption))

    async def photos(self, uid, numbers):
        for n in numbers:
            await self.photo(uid, n)
        await asyncio.sleep(0.02)  # дать сработать обновлению карточки

    def button(self, chat_id, label):
        """Последняя отправленная в чат кнопка с таким текстом: (message_id, callback_data)."""
        for method in reversed(self.session.calls):
            if isinstance(method, (SendMessage, EditMessageText, EditMessageReplyMarkup)) \
                    and method.chat_id in (chat_id, None) and method.reply_markup:
                for row in method.reply_markup.inline_keyboard:
                    for b in row:
                        if label in b.text:
                            return getattr(method, "message_id", None) or self.session.last_ids.get(id(method)), \
                                b.callback_data
        raise AssertionError(f"нет кнопки {label!r} в чате {chat_id}")

    async def press(self, uid, label, chat_id=None):
        chat_id = chat_id or uid
        message_id, data = self.button(chat_id, label)
        await self.press_data(uid, data, message_id or 1, chat_id)

    async def press_data(self, uid, data, message_id=1, chat_id=None):
        chat_id = chat_id or uid
        user = User(id=uid, is_bot=False, first_name=f"u{uid}", username=f"u{uid}")
        msg = Message(message_id=message_id, date=datetime.now(timezone.utc), chat=Chat(id=chat_id, type="private"))
        cb = CallbackQuery(id=str(self.update_id), from_user=user, chat_instance="x", message=msg, data=data)
        await self._feed(callback_query=cb)

    def sent_to(self, chat_id):
        return [m for m in self.session.calls if getattr(m, "chat_id", None) == chat_id]

    def texts_to(self, chat_id):
        return [m.text for m in self.sent_to(chat_id) if isinstance(m, SendMessage)]

    def captions_to(self, chat_id):
        """Текст опубликованных постов: подпись к первому фото альбома."""
        return [album.media[0].caption for album in self.albums_to(chat_id)]

    def albums_to(self, chat_id):
        """Альбомы, которые увидят в чате: отправленные напрямую и пересланные (тогда это их исходник)."""
        albums = []
        for i, m in enumerate(self.session.calls):
            if isinstance(m, SendMediaGroup) and m.chat_id == chat_id:
                albums.append(m)
            elif isinstance(m, ForwardMessages) and m.chat_id == chat_id:
                albums.append(next(x for x in reversed(self.session.calls[:i]) if isinstance(x, SendMediaGroup)))
        return albums

    def forwards_to(self, chat_id):
        return [m for m in self.sent_to(chat_id) if isinstance(m, ForwardMessages)]

    def alerts(self):
        return [m.text for m in self.session.calls if isinstance(m, AnswerCallbackQuery) and m.show_alert]

    def last_draft(self):
        return self.ctx.db.list_drafts(limit=1)[0]

    async def fill_sales(self, uid):
        await self.press(uid, "Создать публикацию") if self._has_button(uid, "Создать публикацию") else None
        await self.press(uid, "Автомобиль на продажу")
        for label_or_text in ("skip", "New Mazda CX-5 High+", "skip", "2026", "Полный", "2.0 бенз.", "150", "АТ", "0",
                              "камера, климат-контроль", "34500", "skip"):
            if label_or_text == "skip":
                await self.press(uid, "Пропустить")
            else:
                await self.text(uid, label_or_text)

    async def fill_auction(self, uid, date=None):
        date = date or (datetime.now() + timedelta(days=30)).strftime("%d.%m.%Y")
        await self.press(uid, "Создать публикацию")
        await self.press(uid, "Автомобиль с аукциона")
        for value in ("Hyundai Elantra, SEL", "skip", "2024/02", "2.0 бензин (150 л.с.)", "Вариатор (IVT)", "Передний",
                      "7902", "ЦЕЛАЯ", "камера заднего вида", date, "7000"):
            if value == "skip":
                await self.press(uid, "Пропустить")
            else:
                await self.text(uid, value)

    def _has_button(self, chat_id, label):
        try:
            self.button(chat_id, label)
            return True
        except AssertionError:
            return False


@lru_cache(maxsize=1)
def dispatcher():
    return build_dispatcher()  # роутер подключается к диспетчеру только один раз


@pytest.fixture
async def h(monkeypatch):
    monkeypatch.setattr(photo_manager, "REFRESH_DELAY", 0)
    session = FakeSession()
    bot = Bot("123456:TEST", session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    cfg = Config(token="x", db_path=":memory:", sales_chat_id="@sales", auction_chat_id="@auction",
                 admin_ids=[ADMIN], manager_ids=[MANAGER])
    db = dbm.Database(":memory:")
    seed_from_config(db, cfg)
    ctx = AppContext(bot=bot, db=db, cfg=cfg)
    ctx.scheduler = PublishScheduler()
    set_ctx(ctx)
    ctx.scheduler.start()
    harness = Harness(ctx, dispatcher(), bot, session)
    await harness.text(ADMIN, "/start")
    yield harness
    ctx.scheduler.shutdown()


async def test_start_shows_spec_menu(h):
    method = [m for m in h.sent_to(ADMIN) if isinstance(m, SendMessage)][-1]
    labels = [b.text for row in method.reply_markup.inline_keyboard for b in row]
    for name in ("Создать публикацию", "Запланированные публикации", "История публикаций", "Настройки"):
        assert any(name in x for x in labels)


async def test_sales_full_flow_to_right_group_with_exactly_ten_photos(h):
    await h.fill_sales(ADMIN)
    d = h.last_draft()
    assert d["type"] == "sales" and d["fields"]["price_ge"] == "34 500$" and d["fields"]["mileage"] == "0 км"

    # 9 фото: нет кнопки завершения, подтверждение запрещено и сказано, сколько осталось
    await h.photos(ADMIN, range(9))
    shown = [m.text for m in h.session.calls if isinstance(m, (SendMessage, EditMessageText))]
    assert any("9/10" in t and "Осталось добавить: <b>1</b>" in t for t in shown)
    assert not h._has_button(ADMIN, "Завершить загрузку")
    await h.press(ADMIN, "Подтвердить")
    assert any("осталось добавить 1" in a for a in h.alerts())
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.DRAFT

    # 10-е фото: бот предлагает завершить. 11-е не добавляется и бот предупреждает
    await h.photos(ADMIN, [9])
    assert h._has_button(ADMIN, "Завершить загрузку")
    await h.photos(ADMIN, [10])
    assert len(h.ctx.db.photos(d["id"])) == 10
    assert any("Максимум 10" in t for t in h.texts_to(ADMIN))
    assert [p["file_id"] for p in h.ctx.db.photos(d["id"])] == [f"file{i}" for i in range(10)]

    # Завершение: предпросмотр альбомом и кнопки «Подтвердить / Редактировать / Отменить»
    await h.press(ADMIN, "Завершить загрузку")
    assert len(h.albums_to(ADMIN)) == 1
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.DRAFT
    for label in ("Подтвердить", "Редактировать", "Отменить"):
        assert h._has_button(ADMIN, label)

    # Двойное нажатие публикует один раз, и подтверждение сразу отправляет пост в группу
    _, data = h.button(ADMIN, "Подтвердить")
    await asyncio.gather(h.press_data(ADMIN, data), h.press_data(ADMIN, data))
    albums = h.albums_to("@sales")
    assert len(albums) == 1 and len(albums[0].media) == 10
    assert [m.media for m in albums[0].media] == [f"file{i}" for i in range(10)]
    assert not h.texts_to("@sales")  # пост цельный: отдельного сообщения с текстом нет
    posts = h.captions_to("@sales")
    assert len(posts) == 1 and "Связь:" in posts[0] and "🛞" in posts[0]
    assert not h.sent_to("@auction")
    stored = h.ctx.db.get_draft(d["id"])
    assert stored["status"] == dbm.PUBLISHED and stored["published_at"]
    assert h.ctx.db.get_publication(d["id"])["text_id"]


async def _ready_auction(h, uid=ADMIN):
    await h.fill_auction(uid)
    await h.photos(uid, range(10))
    return h.last_draft()


async def test_auction_goes_to_auction_group(h):
    d = await _ready_auction(h)
    assert d["type"] == "auction" and d["fields"]["bid"] == "7 000$"
    await h.press(ADMIN, "Подтвердить")
    assert len(h.albums_to("@auction")) == 1 and not h.sent_to("@sales")
    assert "<b>КПП / Привод: Вариатор (IVT) | Передний</b>" in h.captions_to("@auction")[0]


def _in_days(n):
    return (datetime.now() + timedelta(days=n)).strftime("%d.%m.%Y 12:00")


async def test_schedule_reschedule_edit_cancel(h):
    d = await _ready_auction(h)
    await h.press(ADMIN, "Запланировать")

    await h.text(ADMIN, "01.01.2020 10:00")  # прошлое
    assert any("в будущем" in t for t in h.texts_to(ADMIN))
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.APPROVED
    await h.text(ADMIN, _in_days(60))  # позже даты торгов
    assert any("позже даты торгов" in t for t in h.texts_to(ADMIN))

    await h.text(ADMIN, _in_days(5))
    stored = h.ctx.db.get_draft(d["id"])
    assert stored["status"] == dbm.SCHEDULED and h.ctx.scheduler.next_run(d["id"])
    assert any("запланирован" in t and "Asia/Tbilisi" in t for t in h.texts_to(ADMIN))

    # Перенос
    first = stored["scheduled_at"]
    await h.press(ADMIN, "Перенести")
    await h.text(ADMIN, _in_days(6))
    assert h.ctx.db.get_draft(d["id"])["scheduled_at"] != first

    # Просмотр в списке
    await h.press_data(ADMIN, "m:sched")
    assert any("Запланированные публикации" in t and d["code"] in t for t in h.texts_to(ADMIN))

    # Редактирование снимает с расписания и требует нового подтверждения
    await h.press(ADMIN, "Редактировать")
    await h.press(ADMIN, "Пробег")
    await h.text(ADMIN, "8000")
    stored = h.ctx.db.get_draft(d["id"])
    assert stored["status"] == dbm.DRAFT and stored["fields"]["mileage"] == "8 000 км"
    assert h.ctx.scheduler.next_run(d["id"]) is None

    # Отмена запланированной
    await h.press(ADMIN, "Запланировать")
    await h.text(ADMIN, _in_days(5))
    await h.press(ADMIN, "Отменить публикацию")
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.CANCELLED
    assert h.ctx.scheduler.next_run(d["id"]) is None
    assert not h.albums_to("@auction")


async def test_scheduled_job_really_publishes(h):
    from scheduler import _run
    d = await _ready_auction(h)
    await h.press(ADMIN, "Запланировать")
    await h.text(ADMIN, _in_days(5))
    iso = h.ctx.db.get_draft(d["id"])["scheduled_at"]
    await _run(d["id"], "устаревшее время")  # перенесённое задание не срабатывает
    assert not h.albums_to("@auction")
    await _run(d["id"], iso)
    await _run(d["id"], iso)  # повторный запуск не дублирует
    assert len(h.albums_to("@auction")) == 1
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.PUBLISHED


async def test_old_buttons_cannot_change_published_post(h):
    d = await _ready_auction(h)
    _, clear = h.button(ADMIN, "Очистить все")
    _, delete = h.button(ADMIN, "Завершить загрузку")
    await h.press(ADMIN, "Подтвердить")
    version = h.ctx.db.get_draft(d["id"])["version"]
    await h.press_data(ADMIN, clear)
    await h.press_data(ADMIN, f"d:phdel:{d['id']}:0:1")
    await h.press_data(ADMIN, delete)
    stored = h.ctx.db.get_draft(d["id"])
    assert len(h.ctx.db.photos(d["id"])) == 10
    assert stored["status"] == dbm.PUBLISHED and stored["version"] == version


async def test_photo_replace_and_delete_keep_order_and_require_ten(h):
    d = await _ready_auction(h)
    await h.press_data(ADMIN, f"d:phrep:{d['id']}:0:3")
    await h.photos(ADMIN, [77])
    files = [p["file_id"] for p in h.ctx.db.photos(d["id"])]
    assert files[2] == "file77" and len(files) == 10
    await h.press_data(ADMIN, f"d:phdel:{d['id']}:0:1")
    assert len(h.ctx.db.photos(d["id"])) == 9
    await h.press_data(ADMIN, h.button(ADMIN, "Подтвердить")[1])
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.DRAFT
    await h.photos(ADMIN, [78])
    await h.press(ADMIN, "Подтвердить")
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.PUBLISHED


async def test_second_draft_does_not_receive_answers_for_first(h):
    await h.press(ADMIN, "Создать публикацию")
    await h.press(ADMIN, "Автомобиль на продажу")
    first = h.last_draft()
    await h.press(ADMIN, "Создать публикацию")
    await h.press(ADMIN, "Автомобиль с аукциона")
    await h.text(ADMIN, "Hyundai")
    assert h.ctx.db.get_draft(first["id"])["fields"] == {}
    assert h.last_draft()["fields"]["model"] == "Hyundai"


async def test_double_click_on_type_creates_one_draft(h):
    class Once:
        seen = False

    real = h.session.make_request

    async def fake(bot, method, timeout=None):
        if isinstance(method, EditMessageReplyMarkup) and method.reply_markup is None:
            if Once.seen:
                from aiogram.exceptions import TelegramBadRequest
                raise TelegramBadRequest(method=method, message="message is not modified")
            Once.seen = True
        return await real(bot, method, timeout)
    h.session.make_request = fake
    await h.press(ADMIN, "Создать публикацию")
    await h.press(ADMIN, "Автомобиль на продажу")
    await h.press(ADMIN, "Автомобиль на продажу")
    assert len(h.ctx.db.list_drafts()) == 1


async def test_stranger_has_no_access_and_is_not_registered(h):
    before = len(h.session.calls)
    await h.text(STRANGER, "привет")
    await h.text(STRANGER, "/menu")
    await h.text(STRANGER, "/settings")
    assert len(h.session.calls) == before  # полное молчание
    assert h.ctx.role(STRANGER) is None
    await h.press_data(STRANGER, "m:new")
    assert any("Нет доступа" in a for a in h.alerts())
    await h.text(STRANGER, "/start")
    assert any("Доступа к боту нет" in t for t in h.texts_to(STRANGER))
    assert h.ctx.role(STRANGER) is None


async def test_removed_user_not_reregistered_even_with_autoreg(h):
    await h.text(ADMIN, "/autoreg on")
    await h.text(STRANGER, "/start")
    assert h.ctx.role(STRANGER) == "manager"
    h.ctx.db.remove_user(STRANGER)
    await h.text(STRANGER, "/start")
    assert h.ctx.role(STRANGER) is None


async def test_bot_is_silent_in_groups(h):
    before = len(h.session.calls)
    for uid in (ADMIN, MANAGER, STRANGER):
        for text in ("/start", "/menu", "/settings", "/new", "/myid", "привет"):
            await h.text(uid, text, chat_id=-100777, chat_type="supergroup")
    assert len(h.session.calls) == before


async def test_manager_publishes_own_post_without_admin(h):
    await h.text(MANAGER, "/start")
    await h.fill_sales(MANAGER)
    await h.photos(MANAGER, range(10))
    d = h.last_draft()
    assert not h._has_button(MANAGER, "На согласование")
    before = len(h.session.calls)
    await h.press(MANAGER, "Подтвердить")
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.PUBLISHED
    assert h.ctx.db.get_draft(d["id"])["approved_by"] == MANAGER
    assert len(h.albums_to("@sales")) == 1
    assert any("опубликован" in t for t in h.texts_to(MANAGER))  # автор получает отчёт
    # администратору об этом посте ничего не приходит
    assert not [m for m in h.session.calls[before:] if getattr(m, "chat_id", None) == ADMIN]


async def test_preview_goes_to_presser(h):
    await h.text(MANAGER, "/start")
    await h.fill_sales(MANAGER)
    await h.photos(MANAGER, range(10))
    d = h.last_draft()
    albums = len(h.albums_to(ADMIN))
    await h.press_data(ADMIN, f"d:pv:{d['id']}:{d['version']}")
    assert len(h.albums_to(ADMIN)) == albums + 1


async def test_manager_cannot_touch_other_authors_draft(h):
    await h.fill_sales(ADMIN)
    await h.photos(ADMIN, range(10))
    d = h.last_draft()
    await h.text(MANAGER, "/start")
    for act in ("sub", "aps"):
        await h.press_data(MANAGER, f"d:{act}:{d['id']}:{d['version']}")
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.DRAFT and not h.albums_to("@sales")


async def test_old_pending_post_can_still_be_published_by_author(h):
    await h.text(MANAGER, "/start")
    await h.fill_sales(MANAGER)
    await h.photos(MANAGER, range(10))
    d = h.last_draft()
    h.ctx.db.update_draft(d["id"], status=dbm.PENDING)
    await h.press_data(MANAGER, f"d:sub:{d['id']}:{d['version']}")
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.PUBLISHED


async def test_admin_editing_manager_draft_gets_question_in_own_chat(h):
    await h.text(MANAGER, "/start")
    await h.fill_sales(MANAGER)
    d = h.last_draft()
    await h.press_data(ADMIN, f"d:fld:{d['id']}:0:model")
    assert any("Марка и модель" in t for t in h.texts_to(ADMIN))
    await h.text(ADMIN, "Toyota RAV4")
    assert h.ctx.db.get_draft(d["id"])["fields"]["model"] == "Toyota RAV4"


async def test_failed_publication_can_be_fixed_or_cancelled_and_user_notified(h):
    d = await _ready_auction(h)
    h.ctx.db.set_setting("auction_chat_id", "@auction")
    real = h.session.make_request

    async def fail_album(bot, method, timeout=None):
        if isinstance(method, ForwardMessages) and method.chat_id == "@auction":
            from aiogram.exceptions import TelegramForbiddenError
            raise TelegramForbiddenError(method=method, message="bot is not a member <b>")
        return await real(bot, method, timeout)
    h.session.make_request = fail_album
    await h.press(ADMIN, "Подтвердить")
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.FAILED
    assert any("сбой публикации" in t for t in h.texts_to(ADMIN))  # спецсимволы в ошибке не ломают HTML

    await h.press(ADMIN, "Вернуть в черновик")
    stored = h.ctx.db.get_draft(d["id"])
    assert stored["status"] == dbm.DRAFT and stored["approved_by"] is None
    await h.press(ADMIN, "Отменить")
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.CANCELLED


async def test_history_contains_all_spec_fields_and_fits_limit(h):
    long_name = "Очень длинное название " * 3
    for _ in range(16):
        d = h.ctx.db.create_draft("sales", ADMIN, "@a", ADMIN)
        h.ctx.db.update_draft(d["id"], fields={"model": long_name[:80]}, status=dbm.FAILED, error="э" * 500,
                              scheduled_at="2026-12-01T10:00:00+00:00", published_at="2026-12-01T10:00:00+00:00")
        h.ctx.db.save_publication(d["id"], album_ids=[1, 2], text_id=777)
    await h.press_data(ADMIN, "m:h:a:a:0:0")  # check_html также проверяет лимит 4096
    text = h.texts_to(ADMIN)[-1]
    for piece in ("MV-0016", "Автомобиль на продажу", "Актуальные авто", "Создано:", "План:", "Факт:", "Ошибка публикации",
                  "Message ID", "ээээ"):
        assert piece in text
    assert "MV-0001" not in text
    await h.press(ADMIN, "Старее")
    await h.press(ADMIN, "Старее")
    method = [m for m in h.session.calls if isinstance(m, (SendMessage, EditMessageText))][-1]
    assert "MV-0001" in method.text


async def test_settings_commands_render_valid_html(h):
    for cmd in ("/settings", "/users", "/setlink site https://new.example/?a=1&b=2", "/setemoji ✅ 123",
                "/setemoji 🧑‍💻 456", "/setemoji 🦄 1", "/setemoji ✅", "/settz Mars/Base", "/settz Asia/Tbilisi",
                "/setpower off", "/setpower", "/autoreg", "/autoreg off", "/setchat", "/setlink", "/addmanager",
                "/removeuser 1", "/settings"):
        await h.text(ADMIN, cmd)
    assert h.ctx.db.get_setting("custom_emoji") == {"✅": "123", "🧑‍💻": "456"}
    assert h.ctx.db.get_setting("links")["site"] == "https://new.example/?a=1&b=2"
    assert h.ctx.role(ADMIN) == "admin"  # последнего администратора удалить нельзя
    await h.press_data(ADMIN, "m:set")


async def test_custom_emoji_rejected_falls_back_to_plain(h):
    from aiogram.exceptions import TelegramBadRequest
    h.ctx.db.set_setting("custom_emoji", {"✅": "123"})
    d = await _ready_auction(h)
    real = h.session.make_request

    async def reject(bot, method, timeout=None):
        if isinstance(method, SendMediaGroup) and "tg-emoji" in method.media[0].caption:
            raise TelegramBadRequest(method=method, message="ENTITY_TEXT_INVALID")
        return await real(bot, method, timeout)
    h.session.make_request = reject
    await h.press(ADMIN, "Подтвердить")
    posts = h.captions_to("@auction")
    assert len(posts) == 1 and "tg-emoji" not in posts[0] and "✅ <b>Бирка" in posts[0]
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.PUBLISHED


async def test_preview_shows_post_as_one_album(h):
    d = await _ready_auction(h)
    await h.press(ADMIN, "Предпросмотр")
    assert "<b>Бирка: 2024/02</b>" in h.captions_to(ADMIN)[-1]
    assert len(h.albums_to(ADMIN)[-1].media) == 10 and d["id"]


async def test_too_long_post_warns_and_cannot_be_confirmed(h):
    d = await _ready_auction(h)
    h.ctx.db.update_draft(d["id"], fields={**d["fields"], "ps": "П" * 700, "equipment": "О" * 700})
    await h.press(ADMIN, "Предпросмотр")
    assert h.captions_to(ADMIN)[-1] is None  # фото показаны, а текст отдельным сообщением, чтобы его сократить
    assert any("длиннее лимита" in t for t in h.texts_to(ADMIN))
    await h.press(ADMIN, "Подтвердить")
    assert any("длиннее лимита" in a for a in h.alerts())
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.DRAFT


async def test_document_image_gets_a_hint_not_silence(h):
    await h.fill_sales(ADMIN)
    doc = Document(file_id="d", file_unique_id="u", mime_type="image/jpeg")
    await h._feed(message=h._message(ADMIN, ADMIN, document=doc))
    assert any("как фото" in t for t in h.texts_to(ADMIN))
    assert not h.ctx.db.photos(h.last_draft()["id"])


# ---------------------------------------------------------------- заполнение нейросетью
AI_AUCTION = dict(model="Hyundai Elantra, Sel", ps="Комплектация SEL — золотая середина.", tag="2024/02",
                  engine="2.0 бензин (150 л.с.)", gearbox="Вариатор (IVT)", drive="Передний", mileage="7 902 км",
                  safety="ЦЕЛАЯ", equipment="бесключевой доступ", auction_date=None, bid=7000)


def _fake_groq(monkeypatch, answer=None, status=200):
    """Подмена запроса к Groq: answer — поля, которые «вернула» модель."""
    from services import ai_parser
    calls = []

    async def fake_post(api_key, payload):
        calls.append(payload)
        return status, {"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}]}
    monkeypatch.setattr(ai_parser, "_post", fake_post)
    return calls


def _future_date():
    return (datetime.now() + timedelta(days=30)).strftime("%d.%m.%Y")


async def _start_ai_auction(h, uid=ADMIN):
    h.ctx.cfg.groq_api_key = "test"
    await h.press(uid, "Создать с помощью ИИ")
    await h.press(uid, "Автомобиль с аукциона")
    return h.last_draft()


async def test_ai_without_key_is_reported(h):
    await h.press(ADMIN, "Создать с помощью ИИ")
    assert any("GROQ_API_KEY" in t for t in h.texts_to(ADMIN))
    assert not h.ctx.db.list_drafts()


async def test_ai_fills_draft_and_publishes_with_quote(h, monkeypatch):
    calls = _fake_groq(monkeypatch, {**AI_AUCTION, "auction_date": _future_date()})
    d = await _start_ai_auction(h)
    await h.text(ADMIN, "текст объявления")
    assert len(calls) == 1 and calls[0]["messages"][1]["content"] == "текст объявления"
    d = h.ctx.db.get_draft(d["id"])
    assert d["fields"]["bid"] == "7 000$" and d["fields"]["tag"] == "2024/02"
    assert any("<blockquote>" in t and "<b>Бирка: 2024/02</b>" in t for t in h.texts_to(ADMIN))  # предпросмотр текста

    await h.photos(ADMIN, range(10))
    await h.press(ADMIN, "Завершить загрузку")
    assert not h.sent_to("@auction")  # без подтверждения человека нейросеть ничего не публикует
    await h.press(ADMIN, "Подтвердить")
    post = h.captions_to("@auction")[0]
    assert "<blockquote><b>P.S. Комплектация SEL — золотая середина.</b></blockquote>" in post


async def test_ai_album_with_caption_takes_photos_and_text(h, monkeypatch):
    calls = _fake_groq(monkeypatch, {**AI_AUCTION, "auction_date": _future_date()})
    d = await _start_ai_auction(h)
    await h.photo(ADMIN, 0, caption="подпись к альбому")
    await h.photos(ADMIN, range(1, 10))
    assert len(calls) == 1 and calls[0]["messages"][1]["content"] == "подпись к альбому"
    assert len(h.ctx.db.photos(d["id"])) == 10
    assert h.ctx.db.get_draft(d["id"])["fields"]["model"] == "Hyundai Elantra, Sel"
    assert h._has_button(ADMIN, "Завершить загрузку")


async def test_ai_asks_for_missing_required_field(h, monkeypatch):
    _fake_groq(monkeypatch, AI_AUCTION)  # даты торгов в тексте нет
    d = await _start_ai_auction(h)
    await h.text(ADMIN, "текст")
    assert any("Не хватает данных: Дата торгов" in t for t in h.texts_to(ADMIN))
    calls = _fake_groq(monkeypatch, {"auction_date": _future_date(), "model": "Другое название"})
    await h.text(ADMIN, "торги через месяц")
    prompt = calls[0]["messages"][1]["content"]
    assert "Уже известно" in prompt and "Не хватало: Дата торгов" in prompt and "торги через месяц" in prompt
    fields = h.ctx.db.get_draft(d["id"])["fields"]
    assert fields["auction_date"] == _future_date() and fields["ps"]
    assert fields["model"] == "Hyundai Elantra, Sel"  # уже заполненное не перезаписывается
    assert any("Отправьте ровно 10 фото" in t for t in h.texts_to(ADMIN))


async def test_ai_invalid_value_is_not_saved(h, monkeypatch):
    _fake_groq(monkeypatch, {**AI_AUCTION, "auction_date": "01.01.2020"})  # дата уже прошла
    d = await _start_ai_auction(h)
    await h.text(ADMIN, "текст")
    assert "auction_date" not in h.ctx.db.get_draft(d["id"])["fields"]
    assert any("не сохранены: Дата торгов" in t for t in h.texts_to(ADMIN))


async def test_ai_failure_asks_again_and_manual_works(h, monkeypatch):
    _fake_groq(monkeypatch, {}, status=401)
    d = await _start_ai_auction(h)
    await h.text(ADMIN, "текст")
    assert any("не приняла API-ключ" in t and "Пришлите текст" in t for t in h.texts_to(ADMIN))
    assert h.ctx.db.get_draft(d["id"])["fields"] == {}

    _fake_groq(monkeypatch, {"model": None})  # пустой ответ: данных не найдено
    await h.text(ADMIN, "привет")
    assert any("не найдено данных" in t for t in h.texts_to(ADMIN))

    await h.press(ADMIN, "Заполнить вручную")
    await h.text(ADMIN, "Hyundai Elantra")
    assert h.ctx.db.get_draft(d["id"])["fields"]["model"] == "Hyundai Elantra"


async def test_ai_does_not_touch_cancelled_draft(h, monkeypatch):
    from services import ai_parser
    d = await _start_ai_auction(h)

    async def cancel_while_waiting(api_key, payload):
        h.ctx.db.update_draft(d["id"], status=dbm.CANCELLED)
        return 200, {"choices": [{"message": {"content": json.dumps(AI_AUCTION)}}]}
    monkeypatch.setattr(ai_parser, "_post", cancel_while_waiting)
    await h.text(ADMIN, "текст")
    assert h.ctx.db.get_draft(d["id"])["fields"] == {}


def _groq_sequence(monkeypatch, answers):
    """Groq отвечает по очереди заданными (статус, тело); ожидания подменены."""
    from services import ai_parser
    waits, queue = [], list(answers)

    async def fake_post(api_key, payload):
        return queue.pop(0)

    async def fake_sleep(seconds):
        waits.append(seconds)
    monkeypatch.setattr(ai_parser, "_post", fake_post)
    monkeypatch.setattr(ai_parser.asyncio, "sleep", fake_sleep)
    return waits


async def test_ai_rate_limit_waits_and_retries(h, monkeypatch):
    ok = {"choices": [{"message": {"content": json.dumps({**AI_AUCTION, "auction_date": _future_date()})}}]}
    d = await _start_ai_auction(h)
    waits = _groq_sequence(monkeypatch, [(429, {"retry_after": "12"}), (200, ok)])
    await h.text(ADMIN, "текст")
    assert waits == [12.0]
    assert h.ctx.db.get_draft(d["id"])["fields"]["model"] == "Hyundai Elantra, Sel"


async def test_ai_long_rate_limit_asks_to_retry_later(h, monkeypatch):
    d = await _start_ai_auction(h)
    waits = _groq_sequence(monkeypatch, [(429, {"retry_after": "600"})])
    await h.text(ADMIN, "текст")
    assert not waits and any("превышен лимит" in t and "Пришлите текст" in t for t in h.texts_to(ADMIN))
    assert h.ctx.db.get_draft(d["id"])["fields"] == {}


async def test_ai_sales_without_equipment_does_not_ask_for_it(h, monkeypatch):
    _fake_groq(monkeypatch, dict(model="Toyota Prado 250 (42suf)", year="2026", drive="полный", engine="2.4т бензин",
                                 power="276 л.с.", gearbox="автомат", mileage="17 км", price_ge=59500))
    h.ctx.cfg.groq_api_key = "test"
    await h.press(ADMIN, "Создать с помощью ИИ")
    await h.press(ADMIN, "Автомобиль на продажу")
    await h.text(ADMIN, "текст")
    assert not any("Не хватает данных" in t for t in h.texts_to(ADMIN))
    assert any("Отправьте ровно 10 фото" in t for t in h.texts_to(ADMIN))
    assert h.last_draft()["fields"]["equipment"] == ""


async def test_preview_uses_animated_emoji_and_falls_back(h):
    from aiogram.exceptions import TelegramBadRequest
    await _ready_auction(h)
    await h.press(ADMIN, "Предпросмотр")
    assert "tg-emoji" in h.captions_to(ADMIN)[-1]
    real = h.session.make_request

    async def reject(bot, method, timeout=None):
        if isinstance(method, SendMediaGroup) and "tg-emoji" in (method.media[0].caption or ""):
            raise TelegramBadRequest(method=method, message="ENTITY_TEXT_INVALID")
        return await real(bot, method, timeout)
    h.session.make_request = reject
    await h.press(ADMIN, "Предпросмотр")
    assert "tg-emoji" not in h.captions_to(ADMIN)[-1] and "<b>Бирка" in h.captions_to(ADMIN)[-1]


async def test_publish_report_warns_when_emoji_were_stripped(h):
    await _ready_auction(h)
    h.session.strip_emoji = True
    await h.press(ADMIN, "Подтвердить")
    assert any("Анимированные эмодзи не применились" in t for t in h.texts_to(ADMIN))


async def test_help_is_role_specific_and_valid(h):
    await h.text(MANAGER, "/start")
    await h.text(ADMIN, "/help")
    await h.text(MANAGER, "/help")
    assert any("Для администратора" in t and "Подтвердить и опубликовать" in t for t in h.texts_to(ADMIN))
    manager = [t for t in h.texts_to(MANAGER) if "Как работать с ботом" in t][-1]
    assert "Для администратора" not in manager and "Подтвердить и опубликовать" in manager


async def test_help_button_in_menu(h):
    await h.press(ADMIN, "Инструкция")
    assert any("Как работать с ботом" in t for t in h.texts_to(ADMIN))


async def test_stranger_gets_no_help_or_lists(h):
    before = len(h.session.calls)
    for cmd in ("/help", "/drafts", "/pending", "/scheduled", "/history"):
        await h.text(STRANGER, cmd)
    assert len(h.session.calls) == before


async def test_list_commands_show_sections(h):
    await h.fill_sales(ADMIN)
    d = h.last_draft()
    await h.text(ADMIN, "/drafts")
    assert any("Мои черновики" in t and d["code"] in t for t in h.texts_to(ADMIN))
    await h.text(ADMIN, "/scheduled")
    assert any("Запланированные публикации" in t for t in h.texts_to(ADMIN))
    await h.text(ADMIN, "/history")
    assert any("История публикаций" in t and d["code"] in t for t in h.texts_to(ADMIN))
    await h.press(ADMIN, "Ошибки")  # кнопки фильтров истории работают и из команды


async def test_panel_lists_depend_on_role(h):
    from aiogram.methods import DeleteMyCommands, SetChatMenuButton, SetMyCommands
    from aiogram.types import BotCommandScopeAllPrivateChats
    from services.bot_commands import setup_commands

    def panels():
        return {getattr(m.scope, "chat_id", "guests"): [x.command for x in m.commands]
                for m in h.session.calls if isinstance(m, SetMyCommands)}
    await setup_commands(h.bot, h.ctx.db)
    p = panels()
    assert p["guests"] == ["start", "myid"]
    assert "settings" in p[ADMIN] and "users" in p[ADMIN] and "help" in p[ADMIN]
    assert "ai" in p[MANAGER] and "settings" not in p[MANAGER]
    assert any(isinstance(m, SetChatMenuButton) for m in h.session.calls)
    assert any(isinstance(m, SetMyCommands) and isinstance(m.scope, BotCommandScopeAllPrivateChats)
               for m in h.session.calls)  # в группах панели нет

    await h.text(ADMIN, f"/addmanager {STRANGER}")
    assert "ai" in panels()[STRANGER]
    await h.text(ADMIN, f"/removeuser {STRANGER}")
    assert any(isinstance(m, DeleteMyCommands) and m.scope.chat_id == STRANGER for m in h.session.calls)


async def test_confirm_publishes_to_group_of_its_type_only(h):
    d = await _ready_auction(h)
    await h.press(ADMIN, "Подтвердить")
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.PUBLISHED
    assert len(h.albums_to("@auction")) == 1 and not h.albums_to("@sales")
    assert any("опубликован" in t for t in h.texts_to(ADMIN))


async def test_manager_schedules_own_post(h):
    await h.text(MANAGER, "/start")
    await h.fill_sales(MANAGER)
    await h.photos(MANAGER, range(10))
    d = h.last_draft()
    await h.press(MANAGER, "Запланировать")
    assert not h.albums_to("@sales")
    await h.text(MANAGER, _in_days(5))
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.SCHEDULED and h.ctx.scheduler.next_run(d["id"])
    assert not h.albums_to("@sales")


async def test_failed_auto_publish_tells_author(h):
    from aiogram.exceptions import TelegramForbiddenError
    await h.text(MANAGER, "/start")
    await h.fill_sales(MANAGER)
    await h.photos(MANAGER, range(10))
    d = h.last_draft()
    real = h.session.make_request

    async def fail(bot, method, timeout=None):
        if isinstance(method, ForwardMessages) and method.chat_id == "@sales":
            raise TelegramForbiddenError(method=method, message="bot is not a member")
        return await real(bot, method, timeout)
    h.session.make_request = fail
    before = len(h.session.calls)
    await h.press(MANAGER, "Подтвердить")
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.FAILED
    assert any("сбой публикации" in t for t in h.texts_to(MANAGER))
    assert not [m for m in h.session.calls[before:] if getattr(m, "chat_id", None) == ADMIN]
    assert h._has_button(MANAGER, "Повторить публикацию")  # автор может повторить сам
    h.session.make_request = real
    await h.press(MANAGER, "Повторить публикацию")
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.PUBLISHED


async def test_post_is_forwarded_from_admin_dm_with_emoji_and_source_removed(h):
    d = await _ready_auction(h)
    await h.press(ADMIN, "Подтвердить")
    assert len(h.forwards_to("@auction")) == 1 and h.forwards_to("@auction")[0].from_chat_id == ADMIN
    assert not [m for m in h.sent_to("@auction") if isinstance(m, SendMediaGroup)]  # напрямую не отправлялось
    assert "tg-emoji" in h.captions_to("@auction")[0]
    assert any(isinstance(m, DeleteMessages) and m.chat_id == ADMIN for m in h.session.calls)
    pub = h.ctx.db.get_publication(d["id"])
    assert len(pub["album_ids"]) == 10 and pub["text_id"] == pub["album_ids"][0]
    assert not any("не применились" in t for t in h.texts_to(ADMIN))


async def test_without_emoji_in_dm_post_goes_directly_without_forward_label(h):
    await _ready_auction(h)
    h.session.strip_emoji = True
    await h.press(ADMIN, "Подтвердить")
    assert not h.forwards_to("@auction")
    assert "tg-emoji" not in h.captions_to("@auction")[0]
    assert any(isinstance(m, DeleteMessages) and m.chat_id == ADMIN for m in h.session.calls)


async def test_undeliverable_dm_still_publishes_directly(h):
    from aiogram.exceptions import TelegramForbiddenError
    d = await _ready_auction(h)
    real = h.session.make_request

    async def fail_dm(bot, method, timeout=None):
        if isinstance(method, SendMediaGroup) and method.chat_id == ADMIN:
            raise TelegramForbiddenError(method=method, message="bot was blocked")
        return await real(bot, method, timeout)
    h.session.make_request = fail_dm
    await h.press(ADMIN, "Подтвердить")
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.PUBLISHED and not h.forwards_to("@auction")


async def test_failed_forward_removes_source_and_retry_works_once(h):
    from aiogram.exceptions import TelegramForbiddenError
    d = await _ready_auction(h)
    real = h.session.make_request

    async def fail(bot, method, timeout=None):
        if isinstance(method, ForwardMessages):
            raise TelegramForbiddenError(method=method, message="bot is not a member")
        return await real(bot, method, timeout)
    h.session.make_request = fail
    await h.press(ADMIN, "Подтвердить")
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.FAILED
    assert any(isinstance(m, DeleteMessages) and m.chat_id == ADMIN for m in h.session.calls)
    h.session.make_request = real
    await h.press(ADMIN, "Повторить публикацию")
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.PUBLISHED and len(h.forwards_to("@auction")) == 1


def _draft_count(h) -> int:
    return len(h.ctx.db.list_drafts("1=1", ()))


async def test_hidden_ai_is_gone_from_menu_commands_and_old_buttons(h):
    from aiogram.methods import SetMyCommands
    from services.bot_commands import setup_commands
    h.ctx.cfg.hide_ai = True
    calls = len(h.session.calls)
    await h.text(ADMIN, "/menu")
    labels = [b.text for m in h.session.calls[calls:] if getattr(m, "reply_markup", None)
              for row in m.reply_markup.inline_keyboard for b in row]
    assert any("Создать публикацию" in x for x in labels) and not any("ИИ" in x for x in labels)
    await h.text(ADMIN, "/ai")
    assert any("временно недоступна" in t for t in h.texts_to(ADMIN))
    before = _draft_count(h)
    await h.press_data(ADMIN, "m:ait:sales")
    assert _draft_count(h) == before
    await setup_commands(h.bot, h.ctx.db)
    panel = [m for m in h.session.calls if isinstance(m, SetMyCommands) and getattr(m.scope, "chat_id", None) == ADMIN]
    assert "ai" not in [x.command for x in panel[-1].commands] and "new" in [x.command for x in panel[-1].commands]
    await h.text(ADMIN, "/help")
    assert not any("/ai" in t for t in h.texts_to(ADMIN) if "Как работать с ботом" in t)


async def test_hidden_ai_does_not_answer_a_question_asked_earlier(h):
    await h.fill_sales(ADMIN)
    d = h.last_draft()
    h.ctx.db.add_prompt(ADMIN, 999999, d["id"], "ai", replace_kinds=("ai",))
    h.ctx.cfg.hide_ai = True
    await h.text(ADMIN, "Продаю машину")  # без флага ушло бы в нейросеть
    assert any("ИИ временно недоступен" in t for t in h.texts_to(ADMIN))


async def test_hidden_auction_cannot_be_created_or_published(h):
    d = await _ready_auction(h)
    h.ctx.cfg.hide_auction = True
    d = h.ctx.db.get_draft(d["id"])
    for act in ("sub", "aps"):
        await h.press_data(ADMIN, f"d:{act}:{d['id']}:{d['version']}")
    assert h.ctx.db.get_draft(d["id"])["status"] == dbm.DRAFT and not h.albums_to("@auction")

    before = _draft_count(h)
    await h.press_data(ADMIN, "m:nt:auction")
    assert _draft_count(h) == before
    kb_calls = len(h.session.calls)
    await h.text(ADMIN, "/new")
    labels = [b.text for m in h.session.calls[kb_calls:] if getattr(m, "reply_markup", None)
              for row in m.reply_markup.inline_keyboard for b in row]
    assert any("на продажу" in x for x in labels) and not any("аукциона" in x for x in labels)
    await h.text(ADMIN, "/help")
    text = [t for t in h.texts_to(ADMIN) if "Как работать с ботом" in t][-1]
    assert "аукцион" not in text.lower() and "/ai" in text


async def test_hide_flags_are_read_from_env(monkeypatch):
    from config import load_config
    monkeypatch.setenv("BOT_TOKEN", "x")
    monkeypatch.setenv("HIDE_AI", "1")
    monkeypatch.setenv("HIDE_AUCTION", "0")
    cfg = load_config()
    assert cfg.hide_ai is True and cfg.hide_auction is False
