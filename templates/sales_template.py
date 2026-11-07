from services.post_generator import DEFAULT_POWER_SUFFIX, DIVIDER, esc, sales_contacts


def render(f: dict, links: dict, e, power_suffix: str = DEFAULT_POWER_SUFFIX) -> str:
    head = []
    if f.get("color"):
        head.append(f"<blockquote><b>{esc(f['color'])}</b></blockquote>")
    head.append(f"<b>{esc(f['model'])}</b>")
    if f.get("subtitle"):
        head.append(f"({esc(f['subtitle'])})")
    power = esc(f["power"])
    if power_suffix and power_suffix.lower() not in f["power"].lower():  # не дублировать, если уже введён
        power += f", {esc(power_suffix)}"
    ok = e("✅")
    body = [
        f"{ok} Год выпуска: {esc(f['year'])}",
        f"{ok} Привод: {esc(f['drive']).lower()}",
        f"{ok} Двигатель: {esc(f['engine'])}",
        f"{ok} Мощность: {power}",
        f"{ok} Коробка: {esc(f['gearbox'])}",
        f"{ok} Пробег: {esc(f['mileage'])}",
    ]
    equipment = f"\n\nКомплектация: {esc(f['equipment'].rstrip('. '))}.\n" if f.get("equipment") else ""
    prices = [f"<b>Цена в Грузии{e('🇬🇪')}: {esc(f['price_ge'])}</b>"]
    if f.get("price_ru"):
        prices.append(f"<b>Цена под ключ в РФ: {esc(f['price_ru'])}</b>")
    return "\n".join(head) + "\n\n" + "\n".join(body) + equipment + f"\n{DIVIDER}\n" + "\n".join(prices) \
        + f"\n{DIVIDER}\n" + sales_contacts(links, e)
