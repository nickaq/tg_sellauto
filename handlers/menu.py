"""Главное меню, создание публикации, списки."""
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup, Message

import database as dbm
from handlers.ai_fill import ai_enabled, ask_ai_text
from handlers.permissions import PrivateOnly, is_admin, is_staff
from services.cards import STATUS_RU, TYPE_RU, ask_field, fmt_dt, refresh_card
from services.draft_context import get_ctx, user_label
from services.post_generator import esc
from services.validation import get_form

router = Router()
router.message.filter(PrivateOnly())
router.callback_query.filter(PrivateOnly())

MENU_TEXT = ("🚗 <b>MV AUTO BOT</b> — главное меню\n"
             "Все команды открываются кнопкой слева от поля ввода, инструкция: /help")
HELP_MAIN = """📖 <b>Как работать с ботом</b>

<b>1. Создайте пост</b>
🤖 /ai — с помощью ИИ. Выберите тип и пришлите текст в любом виде или перешлите готовый пост. Нейросеть заполнит поля, а если чего-то не хватает, спросит одним сообщением. Отвечать можно тоже одним сообщением.
✍️ /new — вручную. Бот задаёт вопросы по одному полю.

<b>2. Добавьте фото</b>
📷 Нужно ровно 10 фото: по одному или альбомом, как фото, а не файлом. Первое фото будет первым в альбоме. Затем нажмите «✅ Завершить загрузку» и проверьте предпросмотр.

<b>3. Подтвердите</b>
✅ Решение за вами, согласование не нужно: нажмите «Подтвердить и опубликовать» или «🕒 Запланировать» и напишите дату: 10.11.2026 18:30.

<b>4. Публикация</b>
🚀 Бот публикует сразу после нажатия, отменить это нельзя: продажу в канал продаж, аукцион в канал аукционов. Когда пост выйдет, бот пришлёт отчёт.
В канал уходит один пост: альбом из 10 фото с текстом под ним и подписью «Переслано» от бота (только так Telegram сохраняет анимированные эмодзи). Пока пост публикуется, он на секунду появится у вас в личке.

<b>Что важно</b>
• Комплектация обязательна для аукциона, в продаже её можно пропустить.
• Нейросеть не придумывает данные, но перед подтверждением всё равно проверяйте текст.
• Подпись к альбому у Telegram ограничена 1024 символами. Если текст длиннее, бот покажет, сколько сократить.
• Изменить данные: «✏️ Редактировать» в карточке. После правки публикацию нужно подтвердить заново, а расписание снимается.
• Дата торгов пишется как 10.11.2026 и в посте ведёт в Google Calendar.

<b>Команды</b> (кнопка слева от поля ввода)
/menu — панель управления
/drafts — мои черновики
/scheduled — запланированные
/history — история публикаций
/myid — мой Telegram ID"""
HELP_ADMIN = """

<b>Для администратора</b>
/settings — настройки и полный список команд
/users — пользователи
/addmanager &lt;id&gt; — добавить менеджера (свой ID человек узнаёт через /myid)
/addadmin &lt;id&gt; — добавить администратора
/removeuser &lt;id&gt; — отозвать доступ
Бот должен быть администратором канала с правом публиковать сообщения."""
LIST_LIMIT = 20  # с запасом укладывается в лимит сообщения Telegram (4096 символов)
AI_OFF_TEXT = "⚠️ Нейросеть не подключена: укажите GROQ_API_KEY в .env и перезапустите бота."
HIDDEN_TEXT = "⏸ Эта опция временно недоступна."
# Строки инструкции о скрытых опциях (HIDE_AI, HIDE_AUCTION)
HELP_AI_LINES = ("🤖 /ai", "• Нейросеть")
HELP_AUCTION_LINES = ("• Комплектация обязательна для аукциона", "• Дата торгов")


def menu_kb(admin: bool) -> InlineKeyboardMarkup:
    ai = [] if get_ctx().cfg.hide_ai else [[B(text="🤖 Создать с помощью ИИ", callback_data="m:ai")]]
    rows = [[B(text="➕ Создать публикацию", callback_data="m:new")],
            *ai,
            [B(text="🕒 Запланированные публикации", callback_data="m:sched")],
            [B(text="📚 История публикаций", callback_data="m:h:a:a:0:0")],
            [B(text="📝 Мои черновики", callback_data="m:mine")],
            [B(text="📖 Инструкция", callback_data="m:help")]]
    if admin:
        rows.append([B(text="⚙️ Настройки", callback_data="m:set")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def help_text(admin: bool) -> str:
    cfg = get_ctx().cfg
    hidden = (HELP_AI_LINES if cfg.hide_ai else ()) + (HELP_AUCTION_LINES if cfg.hide_auction else ())
    text = "\n".join(line for line in HELP_MAIN.splitlines() if not line.startswith(hidden or ("\0",)))
    if cfg.hide_auction:
        text = text.replace("продажу в канал продаж, аукцион в канал аукционов", "в канал продаж")
    return text + HELP_ADMIN if admin else text


def type_kb(act: str = "nt") -> InlineKeyboardMarkup:
    types = ["sales"] if get_ctx().cfg.hide_auction else ["sales", "auction"]
    return InlineKeyboardMarkup(inline_keyboard=[[B(text=TYPE_RU[t], callback_data=f"m:{act}:{t}")] for t in types])


@router.message(Command("menu"))
async def cmd_menu(m: Message):
    if m.from_user and is_staff(m.from_user.id):
        await m.answer(MENU_TEXT, reply_markup=menu_kb(is_admin(m.from_user.id)))


@router.message(Command("new"))
async def cmd_new(m: Message):
    if m.from_user and is_staff(m.from_user.id):
        await m.answer("🆕 Что создаём?", reply_markup=type_kb())


@router.message(Command("ai"))
async def cmd_ai(m: Message):
    if m.from_user and is_staff(m.from_user.id):
        if get_ctx().cfg.hide_ai:
            return await m.answer(HIDDEN_TEXT)
        if not ai_enabled():
            return await m.answer(AI_OFF_TEXT)
        await m.answer("🤖 Что создаём?", reply_markup=type_kb("ait"))


async def list_message(drafts, title) -> tuple[str, InlineKeyboardMarkup | None]:
    if not drafts:
        return f"{title}\n📭 Пусто.", None
    lines, rows = [f"<b>{title}</b>"], []
    for d in drafts:
        when = f" · {fmt_dt(d['scheduled_at'])}" if d["status"] == dbm.SCHEDULED else ""
        lines.append(f"{d['code']} · {esc(d['fields'].get('model') or '—')} · "
                     f"{STATUS_RU.get(d['status'], d['status'])}{when} · {esc(d['creator_name'] or '')}")
        rows.append([B(text=f"📂 {d['code']} {d['fields'].get('model') or ''}"[:40],
                       callback_data=f"d:open:{d['id']}:0")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _visible(uid: int, status: str):
    c = get_ctx()
    if is_admin(uid):
        return c.db.list_drafts("status=?", (status,), limit=LIST_LIMIT)
    return c.db.list_drafts("status=? AND creator_id=?", (status, uid), limit=LIST_LIMIT)


def _drafts_view(uid: int):
    ds = get_ctx().db.list_drafts(f"creator_id=? AND status IN ({','.join('?' * len(dbm.ACTIVE))})",
                                  (uid, *dbm.ACTIVE), limit=LIST_LIMIT)
    return list_message(ds, "📝 Мои черновики")


def _scheduled_view(uid: int):
    return list_message(_visible(uid, dbm.SCHEDULED), "🕒 Запланированные публикации")


@router.message(Command("help"))
async def cmd_help(m: Message):
    if m.from_user and is_staff(m.from_user.id):
        await m.answer(help_text(is_admin(m.from_user.id)))


async def _answer_list(m: Message, view):
    if m.from_user and is_staff(m.from_user.id):
        text, kb = await view(m.from_user.id)
        await m.answer(text, reply_markup=kb)


@router.message(Command("drafts"))
async def cmd_drafts(m: Message):
    await _answer_list(m, _drafts_view)


@router.message(Command("scheduled"))
async def cmd_scheduled(m: Message):
    await _answer_list(m, _scheduled_view)


@router.callback_query(F.data.startswith("m:") & ~F.data.startswith("m:h:"))
async def on_menu(cb: CallbackQuery):
    c = get_ctx()
    uid = cb.from_user.id
    if not is_staff(uid):
        return await cb.answer("⛔ Нет доступа.", show_alert=True)
    parts = cb.data.split(":")
    act = parts[1]
    if act == "set" and not is_admin(uid):
        return await cb.answer("⛔ Только администратор.", show_alert=True)
    # Кнопки из старых сообщений остаются в чате, поэтому скрытые опции проверяются и здесь
    if (c.cfg.hide_ai and act in ("ai", "ait")) or (c.cfg.hide_auction and act in ("nt", "ait")
                                                    and parts[2] == "auction"):
        return await cb.answer(HIDDEN_TEXT, show_alert=True)
    await cb.answer()
    if act == "new":
        await cb.message.answer("🆕 Что создаём?", reply_markup=type_kb())
    elif act == "ai":
        if not ai_enabled():
            return await cb.message.answer(AI_OFF_TEXT)
        await cb.message.answer("🤖 Что создаём?", reply_markup=type_kb("ait"))
    elif act in ("nt", "ait"):
        try:  # снимаем кнопки: второе нажатие получит ошибку и не создаст лишний черновик
            await cb.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            return
        type_ = parts[2]
        d = c.db.create_draft(type_, uid, user_label(cb.from_user), cb.message.chat.id)
        c.db.audit(uid, d["id"], "create", type_)
        await refresh_card(d["id"], new=True)
        if act == "ait":
            await ask_ai_text(d, cb.message.chat.id)
        else:
            await ask_field(d, get_form(type_)[0])
    elif act == "mine":
        text, kb = await _drafts_view(uid)
        await cb.message.answer(text, reply_markup=kb)
    elif act == "sched":
        text, kb = await _scheduled_view(uid)
        await cb.message.answer(text, reply_markup=kb)
    elif act == "help":
        await cb.message.answer(help_text(is_admin(uid)))
    elif act == "set":
        from handlers.settings import settings_text
        await cb.message.answer(settings_text(), disable_web_page_preview=True)
