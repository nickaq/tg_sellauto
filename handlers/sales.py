"""Форма №1 — автомобиль на продажу."""
from services.validation import Field, bid, mileage, power, rub, text, usd, year  # noqa: F401

FIELDS = [
    Field("color", "Цвет автомобиля", False, "Цвет автомобиля (необязательно).", "Polymetal Grey Metallic (47C)",
          text(40)),
    Field("model", "Марка и модель", True, "Марка и модель.", "Toyota Prado 250 (42suf)", text(80)),
    Field("subtitle", "Подзаголовок", False, "Подзаголовок (необязательно).", "Европа-МАКСИМАЛКА", text(80)),
    Field("year", "Год автомобиля", True, "Год автомобиля.", "2026", year),
    Field("drive", "Привод", True, "Привод.", "Полный", text(40)),
    Field("engine", "Двигатель", True, "Двигатель.", "2.0 бенз.", text(60)),
    Field("power", "Мощность", True, "Мощность.", "150 л.с.", power),
    Field("gearbox", "Коробка передач", True, "Коробка передач.", "АТ", text(40)),
    Field("mileage", "Пробег", True, "Пробег.", "0 км", mileage),
    Field("equipment", "Комплектация", False, "Комплектация (необязательно).", "бесключевой доступ, камера заднего вида",
          text(700)),
    Field("price_ge", "Цена в Грузии", True, "Цена в Грузии (только USD).", "59 500$", usd),
    Field("price_ru", "Цена под ключ в РФ", False, "Цена под ключ в РФ (только RUB, необязательно).",
          "2.5 млн ₽", rub),
]
