import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database as dbm  # noqa: E402
from config import Config  # noqa: E402
from scheduler import PublishScheduler  # noqa: E402
from services.draft_context import AppContext, seed_from_config, set_ctx  # noqa: E402


class FakeBot:
    def __init__(self):
        self.sent = []          # (method, chat_id, payload)
        self.captions = []
        self.fail_album = False
        self._id = 100

    def _next(self):
        self._id += 1
        return self._id

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(("msg", chat_id, text))
        return SimpleNamespace(message_id=self._next())

    async def send_media_group(self, chat_id, media, **kw):
        if self.fail_album:
            raise RuntimeError("album boom")
        self.sent.append(("album", chat_id, len(media)))
        self.captions.append([m.caption for m in media])
        msgs = [SimpleNamespace(message_id=self._next(), media_group_id="g1", caption_entities=[]) for _ in media]
        # Как настоящий Telegram: в личке custom emoji применяются, в каналах бот их теряет.
        if isinstance(chat_id, int) and chat_id > 0 and "tg-emoji" in (media[0].caption or ""):
            msgs[0].caption_entities = [SimpleNamespace(type="custom_emoji")]
        return msgs

    async def forward_messages(self, chat_id, from_chat_id, message_ids, **kw):
        self.sent.append(("forward", chat_id, (from_chat_id, list(message_ids))))
        return [SimpleNamespace(message_id=self._next()) for _ in message_ids]

    async def delete_messages(self, chat_id, message_ids, **kw):
        self.sent.append(("delete", chat_id, list(message_ids)))
        return True

    async def send_photo(self, chat_id, photo, **kw):
        self.sent.append(("album", chat_id, 1))
        return SimpleNamespace(message_id=self._next(), media_group_id=None)

    async def edit_message_text(self, *a, **kw):
        return True

    async def delete_message(self, *a, **kw):
        return True

    async def edit_message_reply_markup(self, *a, **kw):
        return True


@pytest.fixture
def app():
    cfg = Config(token="x", db_path=":memory:", managers_chat_id=-1001, sales_chat_id="@sales",
                 auction_chat_id="@auction", admin_ids=[1], manager_ids=[2, 3])
    db = dbm.Database(":memory:")
    seed_from_config(db, cfg)
    bot = FakeBot()
    ctx = AppContext(bot=bot, db=db, cfg=cfg)
    ctx.scheduler = PublishScheduler()
    set_ctx(ctx)
    return ctx


def fill_sales(ctx, draft_id, photos=10):
    f = dict(color="БЕЛАЯ", model="Mazda CX-5", subtitle="", year="2026", drive="Полный", engine="2.0",
             power="150 л.с.", gearbox="АТ", mileage="0 км", equipment="камера, климат-контроль", price_ge="34.500 $", price_ru="")
    ctx.db.update_draft(draft_id, fields=f)
    for i in range(photos):
        ctx.db.add_photo(draft_id, f"f{i}", f"u{i}", 2, None, 10 + i)
