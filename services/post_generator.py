"""Сборка итогового текста поста (HTML parse mode)."""
import html
import re
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode

DEFAULT_LINKS = {
    "site": "https://mvauto.ge/",
    "telegram": "https://t.me/MASTER_VADYA_GEORGIA",
    "auction_telegram": "http://t.me/master_vadya",
    "whatsapp": "https://wa.me/995595066066",
    "instagram": "https://www.instagram.com/master_vadya?igsh=bWh4Y3ZkdndwcWg2",
    "auction": "https://t.me/master_vadya_auction",
    "youtube": "https://youtube.com/@mastervadyaauto",
    "tiktok": "https://www.tiktok.com/@master_vadya_?_r=1&_t=ZS-97ndkt62bfY",
    "list": "https://t.me/master_vadya/38",
}
LINK_KEYS = list(DEFAULT_LINKS)
DEFAULT_POWER_SUFFIX = ""
DIVIDER = "━━━━━━━━━━━"
CAPTION_LIMIT = 1024  # лимит Telegram на подпись к фото: текст поста идёт подписью к альбому
WHATSAPP_PHONE = "+995 595 066 066"
TELEGRAM_PHONE = "+7 980 392 92 92"
# Анимированные эмодзи из наборов t.me/addemoji/fitauto и t.me/addemoji/ApplicationEmoji.
# Ключ — символ шаблона или назначение значка (один 📱 в шаблонах означает и WhatsApp, и YouTube).
# ✅ и 🚘 намеренно обычные, статичные.
DEFAULT_CUSTOM_EMOJI = {
    "👉": "5375094306145071282",
    "🛞": "5377521684221815300",
    "site": "5359758030198031389",
    "instagram": "5319160079465857105",
    "auction": "5330237710655306682",
    "youtube": "5334681713316479679",
    "tiktok": "5327982530702359565",
    "whatsapp": "5334998226636390258",
    "telegram": "5330237710655306682",
}
# Эмодзи шаблонов: для каждого можно задать custom_emoji_id через /setemoji
TEMPLATE_EMOJI = ("✅", "🇬🇪", "🚘", "👌", "📅", "💰", "📲", "👉", "🧑‍💻", "👛", "🚀", "📱", "📹", "🛞", "💬")


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def link(url: str, label: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{esc(label)}</a>'


def make_emoji(mapping: dict | None, use_custom: bool):
    mapping = mapping or {}

    def e(ch: str, name: str | None = None) -> str:
        cid = mapping.get(ch) or DEFAULT_CUSTOM_EMOJI.get(name) or DEFAULT_CUSTOM_EMOJI.get(ch)
        if use_custom and cid:
            return f'<tg-emoji emoji-id="{esc(cid)}">{ch}</tg-emoji>'
        return ch  # резерв: обычный Unicode
    return e


def calendar_url(model: str, date: str, bid: str = "") -> str | None:
    """Ссылка создания события на весь день в Google Calendar. None, если дата не в формате ДД.ММ.ГГГГ."""
    try:
        day = datetime.strptime(date, "%d.%m.%Y")
    except ValueError:
        return None
    query = urlencode({
        "action": "TEMPLATE",
        "text": f"MV AUTO — Торги {model}",
        "dates": f"{day:%Y%m%d}/{day + timedelta(days=1):%Y%m%d}",
        "details": f"{model}. Ожидаемая выигрышная ставка: {bid}",
    }, quote_via=quote, safe="")
    return f"https://calendar.google.com/calendar/render?{query}"


def _socials(L: dict, e) -> str:
    """Блок соцсетей: один на оба типа объявлений."""
    return "\n".join([
        "<b>Наши соц.сети:</b>",
        f"{e('🧑‍💻', 'site')} {link(L['site'], 'Наш сайт')}",
        f"{e('👛', 'instagram')} {link(L['instagram'], 'Instagram')}",
        f"{e('🚀', 'auction')} {link(L['auction'], 'Авто с аукционов')}",
        f"{e('📱', 'youtube')} {link(L['youtube'], 'YouTube')}",
        f"{e('📹', 'tiktok')} {link(L['tiktok'], 'TikTok')}",
        "",
        f"{e('🛞')} {link(L['list'], 'Master Vadya — Актуальный список авто')}",
    ])


def auction_contacts(links: dict, e=lambda ch: ch) -> str:
    L = {**DEFAULT_LINKS, **(links or {})}
    go = e("👉")
    return "\n".join([
        f"{e('📲')} <b>Запросить расчет стоимости под ключ:</b>",
        f"{go} {link(L['site'], 'Оставить заявку на нашем сайте')}",
        f"{go} {link(L['auction_telegram'], 'Написать в Telegram')}",
        f"{go} {link(L['whatsapp'], 'Написать в WhatsApp')}",
        "",
        _socials(L, e),
    ])


def sales_contacts(links: dict, e=lambda ch: ch) -> str:
    L = {**DEFAULT_LINKS, **(links or {})}
    return "\n".join([
        "Связь:",
        f"{e('📱', 'whatsapp')} {link(L['whatsapp'], WHATSAPP_PHONE)} — WhatsApp",
        f"{e('💬', 'telegram')} {link(L['telegram'], TELEGRAM_PHONE)} — Telegram",
        "",
        _socials(L, e),
    ])


def visible_length(post: str) -> int:
    """Длина текста без разметки в единицах UTF-16: так считается с запасом, эмодзи занимают по 2."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", post)).encode("utf-16-le")) // 2


def build_post(draft: dict, settings: dict, use_custom_emoji: bool = False) -> str:
    from templates.auction_template import render as render_auction
    from templates.sales_template import render as render_sales
    e = make_emoji(settings.get("custom_emoji"), use_custom_emoji)
    links = settings.get("links") or {}
    if draft["type"] == "sales":
        return render_sales(draft["fields"], links, e, settings.get("power_suffix", DEFAULT_POWER_SUFFIX))
    return render_auction(draft["fields"], links, e)
