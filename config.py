"""Конфигурация из .env. Значения из БД (settings) имеют приоритет после первого запуска."""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def parse_ids(value: str) -> list[int]:
    return [int(x) for x in re.split(r"[,\s;]+", value or "") if re.fullmatch(r"-?\d+", x)]


def parse_chat(value: str | None):
    """'-1001234' -> int, '@name' -> '@name', пусто -> None."""
    v = re.sub(r"^(https?://)?t\.me/", "", (value or "").strip()).rstrip("/")
    if not v:
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v if v.startswith("@") else "@" + v


@dataclass
class Config:
    token: str
    db_path: str
    timezone: str = "Asia/Tbilisi"
    managers_chat_id: object = None
    sales_chat_id: object = None
    auction_chat_id: object = None
    admin_ids: list[int] = field(default_factory=list)
    manager_ids: list[int] = field(default_factory=list)
    groq_api_key: str = ""
    groq_model: str = "qwen/qwen3.8-27b"
    hide_ai: bool = False  # временно скрыть создание постов через ИИ
    hide_auction: bool = False  # временно скрыть создание и публикацию аукционов


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN не задан в .env")
    db_path = os.getenv("DB_PATH", "data/mv_auto.db")
    if not os.path.isabs(db_path):
        db_path = str(BASE_DIR / db_path)
    return Config(
        token=token,
        db_path=db_path,
        timezone=os.getenv("TIMEZONE", "Asia/Tbilisi"),
        managers_chat_id=parse_chat(os.getenv("MANAGERS_CHAT_ID")),
        sales_chat_id=parse_chat(os.getenv("SALES_CHAT_ID")),
        auction_chat_id=parse_chat(os.getenv("AUCTION_CHAT_ID")),
        admin_ids=parse_ids(os.getenv("ADMIN_IDS", "")),
        manager_ids=parse_ids(os.getenv("MANAGER_IDS", "")),
        groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
        groq_model=os.getenv("GROQ_MODEL", "").strip() or Config.groq_model,
        hide_ai=_flag("HIDE_AI"),
        hide_auction=_flag("HIDE_AUCTION"),
    )
