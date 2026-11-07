"""Настройки (только администратор, только личный чат). Токен никогда не показывается."""
import logging
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from config import parse_chat
from handlers.permissions import PrivateOnly, is_admin
from services.bot_commands import sync_user_commands
from services.draft_context import get_ctx
from services.post_generator import DEFAULT_LINKS, LINK_KEYS, TEMPLATE_EMOJI, esc

log = logging.getLogger("mvauto")
router = Router()
router.message.filter(PrivateOnly())


def settings_text() -> str:
    c = get_ctx()
    g = c.db.get_setting
    users = c.db.list_users()
    adm = ", ".join(str(u["user_id"]) for u in users if u["role"] == "admin") or "—"
    mgr = ", ".join(str(u["user_id"]) for u in users if u["role"] == "manager") or "—"
    links = {**DEFAULT_LINKS, **g("links", {})}
    emo = g("custom_emoji", {})
    return (
        "⚙️ <b>Настройки</b>\n"
        f"📢 Группа продаж (SALES_CHAT_ID): <code>{esc(g('sales_chat_id'))}</code>\n"
        f"🔨 Группа аукционов (AUCTION_CHAT_ID): <code>{esc(g('auction_chat_id'))}</code>\n"
        f"👥 Рабочая группа (MANAGERS_CHAT_ID, необязательно): <code>{esc(g('managers_chat_id'))}</code>\n"
        f"🌍 Часовой пояс: {esc(c.tz.key)}\n"
        f"⚡ Суффикс мощности: {esc(g('power_suffix', '')) or 'нет'}\n"
        f"👑 Администраторы: {adm}\n🧑‍💼 Менеджеры: {mgr}\n"
        f"🚪 Автодобавление менеджеров: {'включено' if g('auto_register', False) else 'выключено'}\n"
        f"✨ Custom emoji: {esc(', '.join(f'{k}→{v}' for k, v in emo.items())) or 'набор по умолчанию'}\n\n"
        "🔗 <b>Ссылки</b> (влияют только на будущие публикации):\n"
        + "\n".join(f"{k}: {esc(v)}" for k, v in links.items())
        + "\n\n🛠 <b>Команды администратора</b>\n"
        "/addmanager &lt;user_id&gt; · /addadmin &lt;user_id&gt; · /removeuser &lt;user_id&gt; "
        "(или ответом на сообщение человека) · /users\n"
        "/setchat sales|auction|managers &lt;id или @username&gt;\n"
        "/settz &lt;зона, напр. Asia/Tbilisi&gt;\n"
        f"/setlink &lt;{'|'.join(LINK_KEYS)}&gt; &lt;url&gt;\n"
        f"/setemoji &lt;эмодзи&gt; &lt;custom_emoji_id|off&gt; — эмодзи шаблонов: {' '.join(TEMPLATE_EMOJI)}\n"
        "/autoreg on|off — автодобавление написавших боту (по ТЗ выключено)\n"
        "/setpower &lt;текст|off&gt; — суффикс после мощности (по умолчанию нет)")


def _admin(m: Message) -> bool:
    return bool(m.from_user and is_admin(m.from_user.id))


def _log(m, what):
    get_ctx().db.audit(m.from_user.id, None, "config", what)
    log.info("config by %s: %s", m.from_user.id, what)


def _target_user(m: Message, cmd: CommandObject):
    if cmd.args and cmd.args.split()[0].isdigit():
        return int(cmd.args.split()[0]), None
    r = m.reply_to_message
    if r and r.from_user and not r.from_user.is_bot:
        return r.from_user.id, (f"@{r.from_user.username}" if r.from_user.username else r.from_user.full_name)
    return None, None


@router.message(Command("settings"))
async def cmd_settings(m: Message):
    if _admin(m):
        await m.answer(settings_text(), disable_web_page_preview=True)


async def _set_role(m: Message, cmd: CommandObject, role: str):
    if not _admin(m):
        return
    uid, name = _target_user(m, cmd)
    if uid is None:
        return await m.reply("⚠️ Укажите user_id или ответьте командой на сообщение человека.")
    get_ctx().db.set_user(uid, role, name, added_by=m.from_user.id)
    await sync_user_commands(get_ctx().bot, uid, role)
    _log(m, f"{role} {uid}")
    await m.reply(f"✅ Готово: {uid} — {'администратор' if role == 'admin' else 'менеджер'}.")


@router.message(Command("addmanager"))
async def cmd_addmanager(m: Message, command: CommandObject):
    await _set_role(m, command, "manager")


@router.message(Command("addadmin"))
async def cmd_addadmin(m: Message, command: CommandObject):
    await _set_role(m, command, "admin")


@router.message(Command("removeuser"))
async def cmd_removeuser(m: Message, command: CommandObject):
    if not _admin(m):
        return
    c = get_ctx()
    uid, _ = _target_user(m, command)
    if uid is None:
        return await m.reply("⚠️ Укажите user_id или ответьте командой на сообщение человека.")
    admins = [u for u in c.db.list_users() if u["role"] == "admin"]
    if c.role(uid) == "admin" and len(admins) <= 1:
        return await m.reply("🚫 Нельзя удалить последнего администратора.")
    c.db.remove_user(uid)
    await sync_user_commands(c.bot, uid, None)
    _log(m, f"remove {uid}")
    await m.reply(f"🚪 Доступ {uid} отозван.")


@router.message(Command("users"))
async def cmd_users(m: Message):
    if _admin(m):
        us = get_ctx().db.list_users()
        icons = {"admin": "👑", "manager": "🧑‍💼"}
        await m.reply("\n".join(f"{icons.get(u['role'], '👤')} {u['user_id']} — {u['role']} {esc(u['name'] or '')}"
                                for u in us) or "📭 Пусто.")


@router.message(Command("setchat"))
async def cmd_setchat(m: Message, command: CommandObject):
    if not _admin(m):
        return
    c = get_ctx()
    a = (command.args or "").split()
    keys = {"sales": "sales_chat_id", "auction": "auction_chat_id", "managers": "managers_chat_id"}
    if len(a) != 2 or a[0] not in keys or parse_chat(a[1]) is None:
        return await m.reply("ℹ️ Формат: /setchat sales|auction|managers &lt;id или @username&gt;")
    chat = parse_chat(a[1])
    try:
        info = await c.bot.get_chat(chat)
    except Exception as ex:
        return await m.reply(f"⚠️ Не удалось проверить чат: {esc(str(ex))}\nБот должен быть участником чата.")
    c.db.set_setting(keys[a[0]], info.id if isinstance(chat, int) else chat)
    _log(m, f"{keys[a[0]]}={chat} ({info.title})")
    await m.reply(f"✅ {a[0]} → {esc(info.title or str(chat))} (<code>{info.id}</code>)")


@router.message(Command("settz"))
async def cmd_settz(m: Message, command: CommandObject):
    if not _admin(m):
        return
    try:
        ZoneInfo((command.args or "").strip())
    except Exception:
        return await m.reply("⚠️ Неизвестный часовой пояс. Пример: Asia/Tbilisi")
    get_ctx().db.set_setting("timezone", command.args.strip())
    _log(m, f"timezone={command.args.strip()}")
    await m.reply("🌍 Часовой пояс обновлён (уже запланированные публикации выйдут в тот же момент времени).")


@router.message(Command("setlink"))
async def cmd_setlink(m: Message, command: CommandObject):
    if not _admin(m):
        return
    a = (command.args or "").split()
    if len(a) != 2 or a[0] not in LINK_KEYS or not a[1].startswith(("https://", "http://")):
        return await m.reply(f"ℹ️ Формат: /setlink &lt;{'|'.join(LINK_KEYS)}&gt; &lt;https://…&gt;")
    c = get_ctx()
    links = c.db.get_setting("links", {})
    links[a[0]] = a[1]
    c.db.set_setting("links", links)
    _log(m, f"link {a[0]}={a[1]}")
    await m.reply("🔗 Ссылка обновлена (только для будущих публикаций).")


@router.message(Command("setemoji"))
async def cmd_setemoji(m: Message, command: CommandObject):
    if not _admin(m):
        return
    a = (command.args or "").split()
    if len(a) != 2:
        return await m.reply("ℹ️ Формат: /setemoji ✅ &lt;custom_emoji_id|off&gt;")
    key = a[0].replace("️", "")  # вариант с селектором (✅️) приводим к виду из шаблона
    if key not in TEMPLATE_EMOJI:
        return await m.reply(f"⚠️ Этого эмодзи нет в шаблонах. Доступны: {' '.join(TEMPLATE_EMOJI)}")
    c = get_ctx()
    emo = c.db.get_setting("custom_emoji", {})
    if a[1].lower() == "off":
        emo.pop(key, None)
    elif a[1].isdigit():
        emo[key] = a[1]
    else:
        return await m.reply("⚠️ custom_emoji_id — это число.")
    c.db.set_setting("custom_emoji", emo)
    _log(m, f"emoji {key}={a[1]}")
    await m.reply("✨ Готово. Если Telegram не примет custom emoji, пост уйдёт с обычными эмодзи.")


@router.message(Command("setpower"))
async def cmd_setpower(m: Message, command: CommandObject):
    if not _admin(m):
        return
    if not command.args:
        return await m.reply("ℹ️ Формат: /setpower &lt;текст|off&gt;")
    v = "" if command.args.strip().lower() == "off" else command.args.strip()[:30]
    get_ctx().db.set_setting("power_suffix", v)
    _log(m, f"power_suffix={v}")
    await m.reply("✅ Готово.")


@router.message(Command("autoreg"))
async def cmd_autoreg(m: Message, command: CommandObject):
    if not _admin(m):
        return
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off"):
        return await m.reply("ℹ️ Формат: /autoreg on|off")
    get_ctx().db.set_setting("auto_register", arg == "on")
    _log(m, f"auto_register={arg}")
    await m.reply("🚪 Автодобавление менеджеров: " + ("включено" if arg == "on" else "выключено"))
