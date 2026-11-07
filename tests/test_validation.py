import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from services import validation as v


def test_usd_and_rub_formats():
    assert v.usd("34500") == "34 500$"
    assert v.usd("34.500 $") == "34 500$"
    assert v.usd("$ 34 500") == "34 500$"
    assert v.rub("2.5 млн") == "2.5 млн ₽"
    assert v.rub("2500000") == "2.500.000 ₽"
    for bad in ("abc", "0", "34,5"):
        with pytest.raises(ValueError):
            v.usd(bad)


def test_mileage_power_year_tag_bid():
    assert v.mileage("0") == "0 км"
    assert v.mileage("7902") == "7 902 км"
    assert v.mileage("7 902 км") == "7 902 км"
    assert v.power("150") == "150 л.с."
    assert v.year("2026") == "2026"
    with pytest.raises(ValueError):
        v.year("26")
    assert v.tag("2024/02") == "2024/02"
    with pytest.raises(ValueError):
        v.tag("2024/13")
    assert v.bid("7000") == "7 000$"


def test_auction_date_must_be_valid_and_not_past():
    tz = ZoneInfo("Asia/Tbilisi")
    with pytest.raises(ValueError):
        v.auction_date("31.02.2030", tz)
    with pytest.raises(ValueError):
        v.auction_date("01.01.2020", tz)
    assert v.auction_date("10.11.2099", tz) == "10.11.2099"


def test_schedule_parse():
    tz = ZoneInfo("Asia/Tbilisi")
    now = datetime(2026, 11, 7, 12, 0, tzinfo=tz)
    assert v.parse_schedule("10.11.2026 18:30", tz, now).hour == 18
    with pytest.raises(ValueError):
        v.parse_schedule("01.11.2026 18:30", tz, now)
    with pytest.raises(ValueError):
        v.parse_schedule("завтра", tz, now)


def test_photo_rules():
    d = {"type": "sales", "fields": {}}

    def photos(n):
        return [{"file_unique_id": str(i)} for i in range(n)]
    assert any("осталось добавить 1" in x for x in v.validate_for_publish(d, photos(9)))
    assert not any("фото" in x for x in v.validate_for_publish(d, photos(10)))
    assert any("удалите лишние (1)" in x for x in v.validate_for_publish(d, photos(11)))
    assert any("осталось добавить 9" in x for x in v.validate_for_publish(d, photos(1)))
    dup = [{"file_unique_id": "a"}] * 10
    assert any("повтор" in x for x in v.validate_for_publish(d, dup))


def test_currencies_cannot_be_swapped():
    for bad in ("34500 €", "34500 ₽", "2.5 млн"):
        with pytest.raises(ValueError):
            v.usd(bad)
    for bad in ("2500000 $", "2.5 млн $"):
        with pytest.raises(ValueError):
            v.rub(bad)
    with pytest.raises(ValueError):
        v.bid("7000 €")


def test_auction_date_rechecked_before_publish():
    tz = ZoneInfo("Asia/Tbilisi")
    photos = [{"file_unique_id": str(i)} for i in range(10)]
    d = {"type": "auction", "fields": {"auction_date": "01.01.2020"}}
    assert any("прошла" in x for x in v.validate_for_publish(d, photos, tz))
