"""Описание полей форм, нормализация и проверка значений."""
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from services.post_generator import CAPTION_LIMIT, build_post, visible_length

PHOTOS_REQUIRED = 10   # ТЗ: на каждое объявление ровно 10 фото


@dataclass
class Field:
    key: str
    label: str
    required: bool
    prompt: str
    example: str
    normalize: Callable


def _thousands(n: int, sep: str) -> str:
    return f"{n:,}".replace(",", sep)


def _int_amount(s: str):
    """'34.500', '34 500', '34500' -> 34500; иначе None."""
    s = s.strip()
    if re.fullmatch(r"\d+", s):
        return int(s)
    if re.fullmatch(r"\d{1,3}([.,\s ]\d{3})+", s):
        return int(re.sub(r"\D", "", s))
    return None


def text(max_len=200, upper=False):
    def fn(s, tz=None):
        s = " ".join(s.split()) if "\n" not in s else s.strip()
        if not s:
            raise ValueError("Значение не может быть пустым.")
        if len(s) > max_len:
            raise ValueError(f"Слишком длинно (максимум {max_len} символов).")
        return s.upper() if upper else s
    return fn


def year(s, tz=None):
    s = s.strip()
    if not re.fullmatch(r"(19[89]\d|20\d\d)(/(0[1-9]|1[0-2]))?", s) or int(s[:4]) > datetime.now().year + 2:
        raise ValueError("Введите год из 4 цифр, например 2026 (можно с месяцем: 2019/12).")
    return s


def power(s, tz=None):
    s = s.strip()
    if re.fullmatch(r"\d{2,4}", s):
        return f"{s} л.с."
    if not s or len(s) > 40 or not re.search(r"\d", s):
        raise ValueError("Введите мощность, например 150 л.с.")
    return s


def mileage(s, tz=None):
    s = s.strip()
    m = re.fullmatch(r"(\d[\d\s., ]*)\s*(км|km)?\.?", s, re.I)
    if m:
        digits = re.sub(r"\D", "", m.group(1))
        return f"{_thousands(int(digits), ' ')} км"
    if not s or len(s) > 40 or not re.search(r"\d", s):
        raise ValueError("Введите пробег, например 0 км или 7 902 км.")
    return s


def usd(s, tz=None):
    raw = re.sub(r"(?i)\$|usd|долл\w*|дол\b", "", s).strip()
    n = _int_amount(raw)
    if n is None or n <= 0:
        raise ValueError("Введите сумму в долларах США, например 59 500$.")
    return f"{_thousands(n, ' ')}$"


def rub(s, tz=None):
    raw = re.sub(r"(?i)₽|руб\w*|rub|р\.?$", "", s).strip()
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(млн|м)\.?", raw, re.I)
    if m:
        return f"{m.group(1).replace(',', '.')} млн ₽"
    n = _int_amount(raw)
    if n is None or n <= 0:
        raise ValueError("Введите сумму в рублях, например 2.5 млн ₽ или 2500000.")
    return f"{_thousands(n, '.')} ₽"


def bid(s, tz=None):
    raw = re.sub(r"(?i)\$|usd|долл\w*", "", s).strip()
    n = _int_amount(raw)
    if n is None or n <= 0:
        raise ValueError("Введите ставку числом (USD), например 7 000.")
    return f"{_thousands(n, ' ')}$"


def tag(s, tz=None):
    s = s.strip().replace("-", "/").replace(".", "/")
    if not re.fullmatch(r"(19|20)\d\d/(0[1-9]|1[0-2])", s):
        raise ValueError("Бирка в формате ГГГГ/ММ, например 2024/02.")
    return s


def auction_date(s, tz=None):
    try:
        d = datetime.strptime(s.strip(), "%d.%m.%Y")
    except ValueError:
        raise ValueError("Дата торгов в формате ДД.ММ.ГГГГ, например 10.11.2026.")
    today = datetime.now(tz).date() if tz else datetime.now().date()
    if d.date() < today:
        raise ValueError("Дата торгов уже прошла.")
    return d.strftime("%d.%m.%Y")


def get_form(type_: str) -> list[Field]:
    from handlers.auction import FIELDS as A
    from handlers.sales import FIELDS as S
    return S if type_ == "sales" else A


def next_missing(type_, fields: dict):
    for f in get_form(type_):
        if f.key not in fields:
            return f
    return None


def missing_required(type_, fields: dict) -> list[Field]:
    return [f for f in get_form(type_) if f.required and not fields.get(f.key)]


def photos_problem(n: int) -> str | None:
    """Текст проблемы с количеством фото или None, если их ровно 10."""
    if n < PHOTOS_REQUIRED:
        return f"Нужно ровно {PHOTOS_REQUIRED} фото: загружено {n}, осталось добавить {PHOTOS_REQUIRED - n}."
    if n > PHOTOS_REQUIRED:
        return f"Нужно ровно {PHOTOS_REQUIRED} фото: загружено {n}, удалите лишние ({n - PHOTOS_REQUIRED})."
    return None


def caption_overflow(draft: dict, settings: dict | None = None) -> int:
    """На сколько символов текст поста не помещается в подпись к альбому (0 — помещается)."""
    if missing_required(draft["type"], draft["fields"]):
        return 0
    return max(0, visible_length(build_post(draft, settings or {})) - CAPTION_LIMIT)


def overflow_text(n: int) -> str:
    return f"Текст поста длиннее лимита Telegram ({CAPTION_LIMIT}) на {n} симв.: сократите описание."


def validate_for_publish(draft: dict, photos: list, tz: ZoneInfo | None = None,
                         settings: dict | None = None) -> list[str]:
    """Список проблем; пустой — можно отправлять/публиковать. С tz также перепроверяется дата торгов."""
    problems = [f"Не заполнено: {f.label}" for f in missing_required(draft["type"], draft["fields"])]
    count = photos_problem(len(photos))
    if count:
        problems.append(count)
    if len({p["file_unique_id"] for p in photos}) != len(photos):
        problems.append("Есть повторяющиеся фото.")
    over = caption_overflow(draft, settings)
    if over:
        problems.append(overflow_text(over))
    date = draft["fields"].get("auction_date")
    if draft["type"] == "auction" and date and tz:
        try:
            auction_date(date, tz)
        except ValueError as ex:
            problems.append(str(ex))
    return problems


def parse_schedule(s: str, tz: ZoneInfo, now: datetime | None = None) -> datetime:
    """'ДД.ММ.ГГГГ ЧЧ:ММ' в часовом поясе tz -> aware datetime в будущем."""
    try:
        dt = datetime.strptime(" ".join(s.split()), "%d.%m.%Y %H:%M").replace(tzinfo=tz)
    except ValueError:
        raise ValueError("Формат: ДД.ММ.ГГГГ ЧЧ:ММ, например 10.11.2026 18:30.")
    now = now or datetime.now(tz)
    if dt <= now:
        raise ValueError("Время должно быть в будущем.")
    return dt
