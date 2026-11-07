"""Форма №2 — автомобиль с аукциона."""
from services.validation import (Field, auction_date, bid, mileage, tag, text)  # noqa: F401

FIELDS = [
    Field("model", "Марка и модель", True, "Марка и модель.", "Hyundai Elantra, SEL", text(80)),
    Field("ps", "P.S. / краткое описание", False, "P.S. / краткое описание (необязательно).",
          "Описание комплектации SEL", text(700)),
    Field("tag", "Бирка", True, "Бирка (дата выпуска).", "2024/02", tag),
    Field("engine", "ДВС", True, "ДВС.", "2.0 бензин (150 л.с.)", text(80)),
    Field("gearbox", "КПП", True, "КПП.", "Вариатор (IVT)", text(60)),
    Field("drive", "Привод", True, "Привод.", "Передний", text(40)),
    Field("mileage", "Пробег", True, "Пробег.", "7 902 км", mileage),
    Field("safety", "Безопасность", True, "Безопасность.", "ЦЕЛАЯ", text(60)),
    Field("equipment", "Комплектация", True, "Комплектация.", "бесключевой доступ, камера заднего вида",
          text(1500)),
    Field("auction_date", "Дата торгов", True, "Дата торгов (ДД.ММ.ГГГГ).", "10.11.2026", auction_date),
    Field("bid", "Ожидаемая выигрышная ставка", True, "Ожидаемая выигрышная ставка (USD, валюта не нужна).",
          "7 000", bid),
]
