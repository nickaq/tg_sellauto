import html
import re
from urllib.parse import parse_qs, urlparse

from services.post_generator import CAPTION_LIMIT, TEMPLATE_EMOJI, build_post, calendar_url, visible_length
from services.validation import caption_overflow

SALES = dict(color="Polymetal Grey Metallic (47C)", model="New Mazda CX-5 High+", subtitle="Европа-МАКСИМАЛКА",
             year="2026", drive="Полный", engine="2.0 бенз.", power="150 л.с.", gearbox="АТ", mileage="0 км",
             price_ge="34 500$", price_ru="")
AUC = dict(model="Hyundai Elantra, SEL", ps="", tag="2024/02", engine="2.0 бензин (150 л.с.)",
           gearbox="Вариатор (IVT)", drive="Передний", mileage="7 902 км", safety="ЦЕЛАЯ", equipment="",
           auction_date="10.11.2026", bid="7 000$")

# Эталонные подвалы — из инструкции MV AUTO GROUP
SOCIALS = """Наши соц.сети:
🧑‍💻 Наш сайт
👛 Instagram
🚀 Авто с аукционов
📱 YouTube
📹 TikTok

🛞 Master Vadya — Актуальный список авто"""

SALES_CONTACTS = """Связь:
📱 +995 595 066 066 — WhatsApp
💬 +7 980 392 92 92 — Telegram

""" + SOCIALS

AUCTION_CONTACTS = """📲 Запросить расчет стоимости под ключ:
👉 Оставить заявку на нашем сайте
👉 Написать в Telegram
👉 Написать в WhatsApp

""" + SOCIALS

TIKTOK = "https://www.tiktok.com/@master_vadya_?_r=1&amp;_t=ZS-97ndkt62bfY"
INSTAGRAM = "https://www.instagram.com/master_vadya?igsh=bWh4Y3ZkdndwcWg2"
SALES_LINKS = {
    "+995 595 066 066": "https://wa.me/995595066066",
    "+7 980 392 92 92": "https://t.me/MASTER_VADYA_GEORGIA",
    "Наш сайт": "https://mvauto.ge/",
    "Instagram": INSTAGRAM,
    "Авто с аукционов": "https://t.me/master_vadya_auction",
    "YouTube": "https://youtube.com/@mastervadyaauto",
    "TikTok": TIKTOK,
    "Master Vadya — Актуальный список авто": "https://t.me/master_vadya/38",
}
AUCTION_LINKS = {
    "Написать в Telegram": "http://t.me/master_vadya",
    "Написать в WhatsApp": "https://wa.me/995595066066",
    "Наш сайт": "https://mvauto.ge/",
    "Instagram": INSTAGRAM,
    "Авто с аукционов": "https://t.me/master_vadya_auction",
    "YouTube": "https://youtube.com/@mastervadyaauto",
    "TikTok": TIKTOK,
    "Master Vadya — Актуальный список авто": "https://t.me/master_vadya/38",
}


def visible(html_text: str) -> str:
    """То, что увидит читатель: без HTML-разметки."""
    return re.sub(r"<[^>]+>", "", html_text)


def test_sales_matches_example_exactly():
    fields = dict(model="Toyota Prado 250 (42suf)", year="2026", drive="полный", engine="2.4т бензин",
                  power="276 л.с.", gearbox="автомат", mileage="17 км", equipment="камера, климат-контроль",
                  price_ge="59 500$")
    expected = """Toyota Prado 250 (42suf)

✅ Год выпуска: 2026
✅ Привод: полный
✅ Двигатель: 2.4т бензин
✅ Мощность: 276 л.с.
✅ Коробка: автомат
✅ Пробег: 17 км

Комплектация: камера, климат-контроль.

━━━━━━━━━━━
Цена в Грузии🇬🇪: 59 500$
━━━━━━━━━━━
""" + SALES_CONTACTS
    post = build_post({"type": "sales", "fields": fields}, {})
    assert visible(post) == expected
    assert post.startswith("<b>Toyota Prado 250 (42suf)</b>\n\n✅")
    assert "<b>Цена в Грузии🇬🇪: 59 500$</b>" in post


def test_sales_color_is_quote_and_subtitle_in_brackets():
    post = build_post({"type": "sales", "fields": SALES}, {})
    assert post.startswith("<blockquote><b>Polymetal Grey Metallic (47C)</b></blockquote>\n"
                           "<b>New Mazda CX-5 High+</b>\n(Европа-МАКСИМАЛКА)\n\n✅")


def test_auction_matches_example_exactly():
    fields = {**AUC, "ps": "Комплектация SEL — «золотая середина».", "equipment": "бесключевой доступ, старт-стоп."}
    expected = """🚘 Hyundai Elantra, SEL

P.S. Комплектация SEL — «золотая середина».

✅ Бирка: 2024/02
✅ ДВС: 2.0 бензин (150 л.с.)
✅ КПП / Привод: Вариатор (IVT) | Передний
✅ Пробег: 7 902 км
✅ Безопасность: ЦЕЛАЯ 👌

Комплектация: бесключевой доступ, старт-стоп.

📅 Дата торгов: 10.11.2026

💰Ожидаемая выигрышная ставка: 7 000$

""" + AUCTION_CONTACTS
    post = build_post({"type": "auction", "fields": fields}, {})
    assert visible(post) == expected
    assert "<blockquote><b>P.S. Комплектация SEL — «золотая середина».</b></blockquote>" in post
    assert "🚘 <b>Hyundai Elantra, SEL</b>" in post and "✅ <b>Бирка: 2024/02</b>" in post
    assert "<b>💰Ожидаемая выигрышная ставка: 7 000$</b>" in post


def test_ps_prefix_is_not_doubled():
    for ps in ("P.S. Текст", "p.s. Текст", "PS: Текст", "Текст"):
        post = build_post({"type": "auction", "fields": {**AUC, "ps": ps}}, {})
        assert "<blockquote><b>P.S. Текст</b></blockquote>" in post


def test_ps_does_not_eat_words_starting_with_ps():
    post = build_post({"type": "auction", "fields": {**AUC, "ps": "Психология покупателя"}}, {})
    assert "<blockquote><b>P.S. Психология покупателя</b></blockquote>" in post


def test_all_links_clickable_in_both_templates():
    sales = build_post({"type": "sales", "fields": SALES}, {})
    for label, url in SALES_LINKS.items():
        assert f'<a href="{url}">{label}</a>' in sales
    auction = build_post({"type": "auction", "fields": AUC}, {})
    for label, url in AUCTION_LINKS.items():
        assert f'<a href="{url}">{label}</a>' in auction
    assert '👉 <a href="https://mvauto.ge/">Оставить заявку на нашем сайте</a>' in auction


def test_sales_optional_hidden_and_price_block():
    t = build_post({"type": "sales", "fields": {**SALES, "color": "", "subtitle": ""}}, {})
    assert visible(t).startswith("New Mazda CX-5 High+\n\n✅") and "blockquote" not in t
    assert "Цена под ключ в РФ" not in t
    t2 = build_post({"type": "sales", "fields": {**SALES, "price_ru": "2.5 млн ₽"}}, {})
    assert "<b>Цена в Грузии🇬🇪: 34 500$</b>\n<b>Цена под ключ в РФ: 2.5 млн ₽</b>" in t2


def test_power_suffix_off_by_default_not_duplicated_and_configurable():
    t = build_post({"type": "sales", "fields": SALES}, {})
    assert "✅ Мощность: 150 л.с.\n" in t and "Euro-6" not in t
    t = build_post({"type": "sales", "fields": SALES}, {"power_suffix": "Euro-6"})
    assert "✅ Мощность: 150 л.с., Euro-6\n" in t
    t = build_post({"type": "sales", "fields": {**SALES, "power": "150 л.с., Euro-6"}}, {"power_suffix": "Euro-6"})
    assert t.count("Euro-6") == 1


def test_auction_template_merges_gearbox_drive_and_hides_empty():
    t = build_post({"type": "auction", "fields": AUC}, {})
    assert "КПП / Привод: Вариатор (IVT) | Передний" in t
    assert "P.S." not in t and "Комплектация" not in t


def test_auction_mileage_has_no_trailing_dot():
    t = build_post({"type": "auction", "fields": {**AUC, "mileage": "примерно 5000 миль."}}, {})
    assert "✅ <b>Пробег: примерно 5000 миль</b>\n" in t


def test_equipment_ends_with_single_dot():
    for value in ("камера", "камера."):
        for post in (build_post({"type": "auction", "fields": {**AUC, "equipment": value}}, {}),
                     build_post({"type": "sales", "fields": {**SALES, "equipment": value}}, {})):
            assert "Комплектация: камера.\n" in post


def test_ok_hand_only_when_safety_confirmed_intact():
    for safety, shown in (("ЦЕЛАЯ", True), ("целая", True), ("не целая", False), ("после града", False)):
        t = build_post({"type": "auction", "fields": {**AUC, "safety": safety}}, {})
        assert ("👌" in t) is shown


def test_auction_date_links_to_google_calendar():
    t = build_post({"type": "auction", "fields": AUC}, {})
    url = re.search(r'Дата торгов:</b> <a href="([^"]+)">10\.11\.2026</a>', t).group(1)
    q = parse_qs(urlparse(html.unescape(url)).query)
    assert url.startswith("https://calendar.google.com/calendar/render?action=TEMPLATE&amp;text=")
    assert "dates=20261110%2F20261111" in url
    assert q["text"] == ["MV AUTO — Торги Hyundai Elantra, SEL"]
    assert "7 000$" in q["details"][0]


def test_calendar_end_date_crosses_month_and_year():
    assert "dates=20261231%2F20270101" in calendar_url("A", "31.12.2026")
    assert calendar_url("A", "скоро") is None


def test_old_bid_without_dollar_gets_it():
    t = build_post({"type": "auction", "fields": {**AUC, "bid": "7.000"}}, {})
    assert "ставка: 7.000$</b>" in t


def test_custom_emoji_with_fallback_including_contacts_block():
    ids = {ch: str(1000 + i) for i, ch in enumerate(TEMPLATE_EMOJI)}
    s = {"custom_emoji": ids}
    custom = build_post({"type": "auction", "fields": AUC}, s, True)
    sales = build_post({"type": "sales", "fields": SALES}, s, True)
    for ch in TEMPLATE_EMOJI:
        assert f'<tg-emoji emoji-id="{ids[ch]}">{ch}</tg-emoji>' in custom + sales
    plain = build_post({"type": "auction", "fields": AUC}, s, False)
    assert "tg-emoji" not in plain
    assert visible(custom) == visible(plain)  # ссылки и текст не зависят от custom emoji
    assert custom.count("<a href=") == plain.count("<a href=") == 10


def test_custom_links_affect_only_new_posts_text():
    t = build_post({"type": "sales", "fields": SALES}, {"links": {"site": "https://new.example/"}})
    assert 'href="https://new.example/"' in t and 'href="https://mvauto.ge/"' not in t


def test_html_escaped():
    t = build_post({"type": "sales", "fields": {**SALES, "model": "A<b>&"}}, {})
    assert "A&lt;b&gt;&amp;" in t


def test_example_posts_fit_album_caption():
    prado = dict(model="Toyota Prado 250 (42suf)", year="2026", drive="полный", engine="2.4т бензин",
                 power="276 л.с.", gearbox="автомат", mileage="17 км", equipment="О" * 300, price_ge="59 500$")
    elantra = {**AUC, "ps": "К" * 200, "equipment": "О" * 300}  # пределы из подсказок нейросети
    for post in (build_post({"type": "sales", "fields": prado}, {}), build_post({"type": "auction", "fields": elantra}, {})):
        assert visible_length(post) <= CAPTION_LIMIT


def test_visible_length_ignores_markup_and_counts_entities_once():
    assert visible_length('<b>A&amp;B</b> <a href="https://x">y</a>') == 5
    assert visible_length("✅🚘") == 3  # ✅ — 1 единица UTF-16, 🚘 — 2


def test_caption_overflow_reports_excess():
    d = {"type": "auction", "fields": {**AUC, "ps": "П" * 700, "equipment": "О" * 1000}}
    over = caption_overflow(d)
    assert over == visible_length(build_post(d, {})) - CAPTION_LIMIT and over > 0
    assert caption_overflow({"type": "auction", "fields": AUC}) == 0
    assert caption_overflow({"type": "auction", "fields": {}}) == 0  # не заполнено — считать нечего


def test_sales_without_equipment_has_no_empty_block():
    t = build_post({"type": "sales", "fields": {**SALES, "equipment": ""}}, {})
    assert "Комплектация" not in t and "✅ Пробег: 0 км\n━━━━━━━━━━━\n" in t


def test_equipment_required_only_for_auction():
    from services.validation import get_form
    required = {t: next(f for f in get_form(t) if f.key == "equipment").required for t in ("sales", "auction")}
    assert required == {"sales": False, "auction": True}


def test_default_animated_emoji_used_only_when_allowed():
    custom = build_post({"type": "sales", "fields": SALES}, {}, True)
    assert '<tg-emoji emoji-id="5377521684221815300">🛞</tg-emoji>' in custom  # огненное колесо
    assert "tg-emoji" not in build_post({"type": "sales", "fields": SALES}, {}, False)
    assert visible(custom) == visible(build_post({"type": "sales", "fields": SALES}, {}, False))


def test_same_symbol_gets_icon_by_purpose():
    sales = build_post({"type": "sales", "fields": SALES}, {}, True)
    assert '<tg-emoji emoji-id="5334998226636390258">📱</tg-emoji> <a href="https://wa.me/' in sales  # WhatsApp
    assert '<tg-emoji emoji-id="5334681713316479679">📱</tg-emoji> <a href="https://youtube' in sales  # YouTube


def test_check_marks_and_car_stay_static_by_default():
    t = build_post({"type": "auction", "fields": AUC}, {}, True)
    assert t.startswith("🚘 <b>Hyundai") and "✅ <b>Бирка" in t and "\n✅ <b>Безопасность" in t
    assert "✅ Год выпуска" in build_post({"type": "sales", "fields": SALES}, {}, True)


def test_all_three_auction_arrows_are_the_same_outside_links():
    t = build_post({"type": "auction", "fields": AUC}, {}, True)
    arrow = '<tg-emoji emoji-id="5375094306145071282">👉</tg-emoji> <a href='
    assert t.count(arrow) == 3
    assert ">👉" not in t.replace('5375094306145071282">👉', "")


def test_setemoji_overrides_default():
    t = build_post({"type": "sales", "fields": SALES}, {"custom_emoji": {"✅": "777", "📱": "888"}}, True)
    assert '<tg-emoji emoji-id="777">✅</tg-emoji>' in t
    assert '<tg-emoji emoji-id="888">📱</tg-emoji> <a href="https://wa.me/' in t
