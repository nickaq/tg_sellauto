"""Приём фото: ровно 10, порядок, альбомы, замена/удаление, защита от дублей."""
import asyncio
import logging

import database as dbm
from services.cards import refresh_card, repost_photo_prompt, update_photo_prompt
from services.draft_context import can_edit_draft, get_ctx, touch
from services.validation import PHOTOS_REQUIRED

log = logging.getLogger("mvauto")
REFRESH_DELAY = 1.2  # сек; альбом приходит пачкой отдельных сообщений
_debounce: dict[int, asyncio.Task] = {}
_notified_groups: set[str] = set()


def schedule_refresh(draft_id: int):
    """Обновить счётчик и карточку один раз после серии фото."""
    old = _debounce.get(draft_id)
    if old and not old.done():
        old.cancel()

    async def job():
        try:
            await asyncio.sleep(REFRESH_DELAY)
        except asyncio.CancelledError:
            return
        # Дальше отмена запрещена: прерванная отправка оставила бы лишнюю карточку в чате.
        _debounce.pop(draft_id, None)
        try:
            if len(get_ctx().db.photos(draft_id)) == PHOTOS_REQUIRED:
                await refresh_card(draft_id)
                await repost_photo_prompt(draft_id)  # предложение завершить загрузку — внизу диалога
            else:
                await update_photo_prompt(draft_id)
                await refresh_card(draft_id, new=True)  # свежая карточка внизу диалога с кнопками
        except Exception:
            log.exception("photo refresh failed")
    _debounce[draft_id] = asyncio.create_task(job())


async def handle_photo(m, prompt: dict):
    c = get_ctx()
    uid = m.from_user.id
    draft_id = prompt["draft_id"]
    async with c.lock(draft_id):
        d = c.db.get_draft(draft_id)
        if not d or not can_edit_draft(d, uid):
            return  # чужое фото не попадает в объявление
        if d["status"] not in dbm.EDITABLE:
            await m.reply(f"🔒 {d['code']}: изменение фото недоступно в этом статусе.")
            return
        ph = m.photo[-1]
        photos = c.db.photos(draft_id)
        if any(p["file_unique_id"] == ph.file_unique_id for p in photos):
            await m.reply("♻️ Это фото уже есть в черновике — дубликат не добавлен.")
            return
        rp = d.get("replace_pos")
        if rp and 1 <= rp <= len(photos):
            c.db.replace_photo(photos[rp - 1]["id"], ph.file_id, ph.file_unique_id, uid,
                               m.media_group_id, m.message_id)
            touch(draft_id, uid, replace_pos=None)
            c.db.audit(uid, draft_id, "photo_replace", f"№{rp}")
        elif len(photos) >= PHOTOS_REQUIRED:
            key = m.media_group_id
            if key is None or key not in _notified_groups:
                if key:
                    _notified_groups.add(key)
                await m.reply(f"🚫 Максимум {PHOTOS_REQUIRED} фото — лишние не добавляются. "
                              "Чтобы заменить фото — кнопки «Заменить» и «Удалить».")
            return
        else:
            c.db.add_photo(draft_id, ph.file_id, ph.file_unique_id, uid, m.media_group_id, m.message_id)
            touch(draft_id, uid)
            c.db.audit(uid, draft_id, "photo_add", f"msg={m.message_id}")
    schedule_refresh(draft_id)


async def delete_photo(draft_id: int, pos: int, uid: int) -> bool:
    c = get_ctx()
    async with c.lock(draft_id):
        d = c.db.get_draft(draft_id)
        photos = c.db.photos(draft_id)
        if d["status"] not in dbm.EDITABLE or not 1 <= pos <= len(photos):
            return False
        c.db.delete_photo(photos[pos - 1]["id"])
        touch(draft_id, uid, replace_pos=None)
        c.db.audit(uid, draft_id, "photo_delete", f"№{pos}")
    return True


async def clear_photos(draft_id: int, uid: int) -> bool:
    c = get_ctx()
    async with c.lock(draft_id):
        if c.db.get_draft(draft_id)["status"] not in dbm.EDITABLE:
            return False
        c.db.clear_photos(draft_id)
        touch(draft_id, uid, replace_pos=None)
        c.db.audit(uid, draft_id, "photos_clear")
    return True
