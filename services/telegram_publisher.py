"""Публикация в целевую группу: один пост — альбом из 10 фото с текстом в подписи, пересылкой из лички."""
import asyncio
import logging

from aiogram.exceptions import TelegramRetryAfter

import database as dbm
from config import parse_chat
from services.cards import GROUP_RU, album_media, fmt_dt, refresh_card
from services.draft_context import get_ctx
from services.post_generator import build_post, esc
from services.validation import validate_for_publish

log = logging.getLogger("mvauto")
MAX_SEND_ATTEMPTS = 3
MAX_RETRY_AFTER = 60  # сек; дольше не держим блокировку — повтор вручную


def post_link(chat_id, message_id: int) -> str | None:
    if isinstance(chat_id, str) and chat_id.startswith("@"):
        return f"https://t.me/{chat_id[1:]}/{message_id}"
    if isinstance(chat_id, int) and str(chat_id).startswith("-100"):
        return f"https://t.me/c/{str(chat_id)[4:]}/{message_id}"
    return None


async def _send_limited(send):
    """Автоповтор только при flood control: Telegram точно отверг запрос, поэтому дубликата не будет.
    Сетевые ошибки автоматически не повторяются — сообщение могло уже уйти."""
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            return await send()
        except TelegramRetryAfter as ex:
            if attempt == MAX_SEND_ATTEMPTS or ex.retry_after > MAX_RETRY_AFTER:
                raise
            log.warning("flood control: повтор через %s с (попытка %s)", ex.retry_after, attempt)
            await asyncio.sleep(ex.retry_after)


async def _safe_send(chat_id, text):
    try:
        await get_ctx().bot.send_message(chat_id, text, disable_web_page_preview=True)
    except Exception as ex:
        log.warning("send to %s failed: %s", chat_id, ex)


async def notify_participants(d: dict, text: str):
    """Автору, подтвердившему и в рабочую группу, если она задана. Администратору — только если это его пост."""
    for chat in {d["manager_chat_id"], d["approved_by"], get_ctx().managers_chat_id} - {None}:
        await _safe_send(chat, text)


async def _deliver(d: dict, photos: list, target, settings: dict) -> tuple[list[int], bool]:
    """Доставка поста. Возвращает (message_id в группе, потеряны ли анимированные эмодзи).

    В каналах Telegram убирает custom emoji у ботов при отправке, копировании и правке; сохраняет только пересылка.
    Поэтому пост собирается в личке подтвердившего, пересылается в группу, а исходник удаляется.
    Если в личке эмодзи не применились, пересылка не нужна: пост уходит напрямую, без подписи «Переслано».
    """
    bot = get_ctx().bot
    source = d["approved_by"]
    try:
        media = album_media(photos, build_post(d, settings, True))
        sent = await _send_limited(lambda: bot.send_media_group(source, media, disable_notification=True))
    except Exception as ex:  # в личку не доставлено: в группе ещё ничего нет, поэтому можно отправить напрямую
        log.warning("%s: не удалось собрать пост в личке %s: %s", d["code"], source, ex)
        sent = None
    if sent:
        ids = [m.message_id for m in sent]
        has_emoji = any(x.type == "custom_emoji" for x in sent[0].caption_entities or [])
        try:
            if has_emoji:
                forwarded = await _send_limited(lambda: bot.forward_messages(target, source, ids))
                return [m.message_id for m in forwarded], False
        finally:
            try:
                await bot.delete_messages(source, ids)
            except Exception as ex:
                log.warning("%s: исходник в личке %s не удалён: %s", d["code"], source, ex)
    media = album_media(photos, build_post(d, settings, False))
    msgs = await _send_limited(lambda: bot.send_media_group(target, media))
    return [m.message_id for m in msgs], True


async def publish_draft(draft_id: int, by_user: int | None = None) -> tuple[bool, str]:
    """Идемпотентная публикация. Возвращает (успех, сообщение)."""
    c = get_ctx()
    async with c.lock(draft_id):
        d = c.db.get_draft(draft_id)
        if not d:
            return False, "Заявка не найдена."
        if d["status"] not in (dbm.APPROVED, dbm.SCHEDULED, dbm.FAILED):
            return False, f"Нельзя опубликовать: статус {d['status']}."
        if not d["approved_by"] or d["approved_version"] != d["version"]:
            return False, "Текущая версия не подтверждена."
        photos = c.db.photos(draft_id)
        settings = c.settings_for_post()
        problems = validate_for_publish(d, photos, c.tz, settings)
        if problems:
            return False, "Проверка не пройдена: " + "; ".join(problems)
        pub = c.db.get_publication(draft_id) or {"album_ids": [], "text_id": None}
        emoji_lost = False
        # Текст идёт в ту же группу, что и альбом, даже если настройку групп успели поменять.
        target = parse_chat(pub["target_chat_id"]) if pub["album_ids"] else c.target_chat(d["type"])
        if target is None:
            return False, "Не задана целевая группа (настройки)."
        if not c.db.claim(draft_id, (dbm.APPROVED, dbm.SCHEDULED, dbm.FAILED), dbm.PUBLISHING,
                          target_chat_id=str(target), error=None):
            return False, "Публикация уже выполняется."
        c.db.audit(by_user, draft_id, "publish_start", f"-> {target}")
        c.db.save_publication(draft_id, version=d["version"], target_chat_id=str(target), status="started")

        # Повторно не отправляем, если уже есть message_id.
        if not pub["album_ids"]:
            try:
                ids, emoji_lost = await _deliver(d, photos, target, settings)
            except Exception as ex:
                return await _fail(d, f"Пост не отправлен: {ex}", by_user)
            if emoji_lost:
                log.warning("%s: анимированные эмодзи не применились, в %s ушли обычные", d["code"], target)
            c.db.save_publication(draft_id, album_ids=ids, text_id=ids[0])
        pub = c.db.get_publication(draft_id)
        c.db.save_publication(draft_id, status="published", published_at=dbm.now_iso(), error=None)
        c.db.update_draft(draft_id, status=dbm.PUBLISHED, published_at=dbm.now_iso(), error=None)
        c.db.audit(by_user, draft_id, "published", f"album={pub['album_ids']} text={pub['text_id']}")
        d = c.db.get_draft(draft_id)

    url = post_link(target, pub["text_id"] or pub["album_ids"][0])
    await refresh_card(draft_id)
    report = (f"📣 <b>{d['code']}</b> опубликован в группе {GROUP_RU[d['type']]}\n"
              f"👤 Автор: {esc(d['creator_name'] or '—')} · подтвердил: {esc(d['approved_by_name'] or '—')}\n"
              f"🕒 Время: {fmt_dt(d['published_at'])} ({c.tz.key})"
              + (f"\n🔗 <a href=\"{url}\">Ссылка на пост</a>" if url else "")
              + ("\n⚠️ Анимированные эмодзи не применились, пост вышел с обычными."
                 if emoji_lost else ""))
    await notify_participants(d, report)
    return True, "Опубликовано."


async def _fail(d: dict, error: str, by_user):
    c = get_ctx()
    log.error("publish %s failed: %s", d["code"], error)
    c.db.update_draft(d["id"], status=dbm.FAILED, error=error)
    c.db.save_publication(d["id"], status="failed", error=error)
    c.db.audit(by_user, d["id"], "publish_failed", error)
    await refresh_card(d["id"])
    await notify_participants(d, f"⚠️ <b>{d['code']}</b>: сбой публикации.\n{esc(error[:500])}")
    return False, error
