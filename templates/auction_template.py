import re

from services.post_generator import auction_contacts, calendar_url, esc, link


def render(f: dict, links: dict, e) -> str:
    out = [f"{e('🚘')} <b>{esc(f['model'])}</b>"]
    if f.get("ps"):
        ps = re.sub(r"(?i)^\s*p\.?\s?s\.?[:\s]*", "", f["ps"])  # префикс добавляет шаблон, не дублируем
        out += ["", f"<blockquote><b>P.S. {esc(ps)}</b></blockquote>"]
    ok = e("✅")
    # 👌 только если безопасность подтверждена как целая
    intact = re.search(r"цел", f["safety"], re.I) and not re.search(r"не\s*цел", f["safety"], re.I)
    # ставка раньше хранилась без знака: черновики до этого изменения тоже получают $
    bid = f["bid"].rstrip("$ ") + "$"
    url = calendar_url(f["model"], f["auction_date"], bid)
    date = link(url, f["auction_date"]) if url else esc(f["auction_date"])
    out += [
        "",
        f"{ok} <b>Бирка: {esc(f['tag'])}</b>",
        f"{ok} <b>ДВС: {esc(f['engine'])}</b>",
        f"{ok} <b>КПП / Привод: {esc(f['gearbox'])} | {esc(f['drive'])}</b>",
        f"{ok} <b>Пробег: {esc(f['mileage'].rstrip('. '))}</b>",
        f"{ok} <b>Безопасность: {esc(f['safety'])}</b>" + (f" {e('👌')}" if intact else ""),
    ]
    if f.get("equipment"):
        out += ["", f"Комплектация: {esc(f['equipment'].rstrip('. '))}."]
    out += [
        "",
        f"{e('📅')} <b>Дата торгов:</b> {date}",
        "",
        f"<b>{e('💰')}Ожидаемая выигрышная ставка: {esc(bid)}</b>",
        "",
        auction_contacts(links, e),
    ]
    return "\n".join(out)
