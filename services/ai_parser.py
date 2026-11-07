"""Разбор текста объявления нейросетью (Groq).

Модель возвращает только значения полей в JSON. Они проходят те же проверки, что и ручной ввод,
а HTML собирают шаблоны: разметку, ссылки и цитату нейросеть испортить не может.
"""
import asyncio
import json
import logging
import ssl
from datetime import datetime

import aiohttp
import certifi

from config import BASE_DIR
from services.validation import get_form

log = logging.getLogger("mvauto")

RULES_PATH = BASE_DIR / "prompts" / "mv_auto_rules.md"  # правила можно менять без правки кода
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
TIMEOUT = 60        # сек на один запрос
ATTEMPTS = 2
MAX_INPUT = 6000    # символов текста объявления
MAX_ANSWER_TOKENS = 800  # ответу хватает ~150; меньше запас — меньше расход минутного лимита токенов Groq
MAX_RATE_WAIT = 30  # сек: дольше не держим менеджера, просим повторить позже

HINTS = {
    "sales": {
        "color": 'цвет кузова, только если указан, например "Polymetal Grey Metallic (47C)"',
        "model": 'марка, модель, модификация и комплектация полностью, вместе с обозначениями в скобках, например '
                 '"Toyota Crown Signia AWD Limited" или "Toyota Prado 250 (42suf)". '
                 'Если марка не указана, но однозначно следует из модели, добавь её',
        "subtitle": 'подзаголовок без скобок, только если указан, например "Европа-МАКСИМАЛКА"',
        "year": '"2026" или с месяцем "2019/12"',
        "drive": 'например "полный"',
        "engine": 'например "2.5 гибрид"',
        "power": 'в л.с., например "276 л.с."',
        "gearbox": 'например "8-ступенчатый автомат" или "вариатор"',
        "mileage": 'например "7 902 км" или "5 000 миль"',
        "equipment": "подтверждённый перечень оборудования через запятую, без слова «Комплектация», только если "
                     "он есть в тексте. Не длиннее 300 символов",
        "price_ge": "цена в Грузии в долларах, только число",
        "price_ru": 'цена под ключ в РФ в рублях, только если указана, например "2.5 млн"',
    },
    "auction": {
        "model": 'марка, модель, модификация и комплектация полностью, вместе с обозначениями в скобках, например '
                 '"Hyundai Elantra SEL". '
                 'Если марка не указана, но однозначно следует из модели, добавь её',
        "ps": "краткое описание без префикса P.S., только если оно есть в тексте: не сочиняй его. "
              "Не длиннее 200 символов",
        "tag": 'бирка, дата выпуска в формате ГГГГ/ММ, например "2024/02"',
        "engine": 'двигатель и подтверждённая мощность, например "2.0 бензин (150 л.с.)"',
        "gearbox": 'например "вариатор (IVT)" или "8-ступенчатый автомат"',
        "drive": 'например "передний"',
        "mileage": 'например "7 902 км" или "5 000 миль"',
        "safety": 'состояние, только если указано в тексте, например "ЦЕЛАЯ"',
        "equipment": "подтверждённый перечень оборудования через запятую, без слова «Комплектация». "
                     "Не длиннее 300 символов",
        "auction_date": "дата торгов в формате ДД.ММ.ГГГГ",
        "bid": "ожидаемая выигрышная ставка в долларах, только число",
    },
}


class AIError(Exception):
    """Текст ошибки можно показать пользователю."""


def build_messages(type_: str, text: str, today: str) -> list[dict]:
    keys = "\n".join(f'- "{f.key}": {HINTS[type_][f.key]}' for f in get_form(type_))
    system = (RULES_PATH.read_text(encoding="utf-8").strip() + "\n\n━━━━━━━━━━━━━━━━━━━\n\n"
              f"ФОРМАТ ОТВЕТА\nСегодня {today}. Верни один JSON-объект строго с этими ключами:\n{keys}\n"
              "Если значения нет, верни для ключа null.")
    return [{"role": "system", "content": system}, {"role": "user", "content": text[:MAX_INPUT]}]


async def _post(api_key: str, payload: dict) -> tuple[int, dict]:
    """Отдельная функция: тесты подменяют её, чтобы не ходить в сеть."""
    headers = {"Authorization": f"Bearer {api_key}"}
    # Сертификаты из certifi, как в aiogram: системное хранилище Python на macOS часто пустое.
    connector = aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=certifi.where()))
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as session:
        async with session.post(GROQ_URL, json=payload, headers=headers) as r:
            data = await r.json(content_type=None)
            if r.status == 429 and isinstance(data, dict):
                data["retry_after"] = r.headers.get("retry-after")
            return r.status, data


def _retry_after(data) -> float:
    try:
        return float(data.get("retry_after"))
    except (AttributeError, TypeError, ValueError):
        return 5.0  # Groq не сказал, сколько ждать


async def request_fields(api_key: str, model: str, type_: str, text: str, today: str) -> dict:
    """Сырые значения полей от модели. При сбое — AIError с понятным текстом."""
    # Низкая температура: нужны точные данные из текста, а не творчество.
    payload = {"model": model, "temperature": 0.1, "top_p": 0.95, "max_completion_tokens": MAX_ANSWER_TOKENS,
               "reasoning_effort": "default", "response_format": {"type": "json_object"},
               "messages": build_messages(type_, text, today)}
    problem = "сервис нейросети не ответил"
    for attempt in range(1, ATTEMPTS + 1):
        try:
            status, data = await _post(api_key, payload)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as ex:
            log.warning("Groq: запрос не удался (%s): %s", attempt, type(ex).__name__)
            continue
        if status in (401, 403):
            raise AIError("нейросеть не приняла API-ключ")
        if status == 429:
            wait = _retry_after(data)
            if attempt == ATTEMPTS or wait > MAX_RATE_WAIT:
                raise AIError("превышен лимит запросов к нейросети, попробуйте через минуту")
            log.info("Groq: лимит запросов, повтор через %s с", wait)
            await asyncio.sleep(wait)
            continue
        if status != 200:
            log.warning("Groq: HTTP %s: %s", status, str(data)[:300])
            problem = f"сервис нейросети вернул ошибку {status}"
            continue
        try:
            result = json.loads(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, ValueError):
            log.warning("Groq: неожиданный ответ: %s", str(data)[:300])
            problem = "нейросеть вернула некорректный ответ"
            continue
        if isinstance(result, dict):
            return result
        problem = "нейросеть вернула некорректный ответ"
    raise AIError(problem)


def apply_fields(type_: str, raw: dict, tz=None) -> tuple[dict, list[str]]:
    """Прогоняет значения через валидаторы формы.

    Необязательные поля без значения становятся пустыми, чтобы бот не спрашивал о них.
    Обязательные остаются незаполненными: их бот спросит обычным вопросом.
    Возвращает (поля, названия полей с отброшенным неверным значением).
    """
    fields, invalid = {}, []
    for f in get_form(type_):
        value = raw.get(f.key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if isinstance(value, str) and value.strip() and value.strip().lower() not in ("null", "none"):
            try:
                fields[f.key] = f.normalize(value, tz)
                continue
            except ValueError:
                invalid.append(f.label)
        if not f.required:
            fields[f.key] = ""
    return fields, invalid


async def parse_ad(api_key: str, model: str, type_: str, text: str, tz=None) -> tuple[dict, list[str]]:
    today = datetime.now(tz).strftime("%d.%m.%Y")
    return apply_fields(type_, await request_fields(api_key, model, type_, text, today), tz)
