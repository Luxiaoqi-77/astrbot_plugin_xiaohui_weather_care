"""离线验证官方区级预报解析、日期、雨雪判断及不编造概率。"""

from datetime import datetime, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from logic import (
    DayRow,
    build_url,
    parse_daily,
    evaluate,
    build_reminder_content,
    describe_forecast,
)

NOW = datetime(2026, 12, 31, 21, tzinfo=ZoneInfo("Asia/Shanghai"))


def page(weather="多云", night_only=False):
    items = []
    for i in range(7):
        date = NOW.date() + timedelta(days=i)
        text = weather if i == 1 else "多云"
        day = '<big class="png40 d01"></big>'
        high = "<span>30℃</span>/"
        if i == 0 and night_only:
            day, high = '<big class="png40"></big>', ""
        items.append(
            f'<li class="sky skyid lv2"><h1>{date.day}日（预报）</h1>{day}<big class="png40 n01"></big><p class="wea" title="{text}">{text}</p><p class="tem">{high}<i>22℃</i></p></li>'
        )
    return (
        '<title>天河天气预报,天河7天天气预报</title><input id="fc_24h_internal_update_time" value="2026123120"/>'
        + "".join(items)
    )


def test_district_url():
    assert (
        build_url("101280109") == "https://www.weather.com.cn/weather/101280109.shtml"
    )
    with pytest.raises(ValueError):
        build_url("../101280109?x=1")


def test_parse_night_and_year_boundary():
    rows = parse_daily(page(night_only=True), "天河", NOW, 7)
    assert rows[0].tmax is None
    assert rows[0].day_text.startswith("未知")
    assert rows[0].tmin == 22
    assert rows[1] == DayRow("2027-01-01", 30, 22, "多云", "多云")
    assert rows[-1].date == "2027-01-06"


@pytest.mark.parametrize("weather", ["多云", "晴", "阴", "雾", "霾", "沙尘暴"])
def test_dry_forecast_does_not_trigger(weather):
    rows = parse_daily(page(weather), "天河", NOW)
    assert not evaluate(rows[0], rows[1], 3).triggered


@pytest.mark.parametrize(
    "weather", ["小雨", "雷阵雨", "多云转小雨", "小雨转晴", "雨夹雪", "小雪"]
)
def test_rain_or_snow_forecast_triggers_without_probability(weather):
    rows = parse_daily(page(weather), "天河", NOW)
    verdict = evaluate(rows[0], rows[1], 3)
    assert verdict.precip_hit
    content = build_reminder_content("天河", rows[0], rows[1], verdict)
    assert "带伞" in content
    assert "%" not in content
    assert "中央气象台" in content
    if weather == "多云转小雨":
        assert "白天多云、夜间小雨" in content


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.replace("天河天气", "海珠天气"),
        lambda s: s.replace("2026123120", "2026122920"),
        lambda s: s.replace("2026123120", "2027010120"),
        lambda s: s.replace('id="fc_24h_internal_update_time"', 'id="removed"'),
        lambda s: s.replace("1日（预报）", "15日（预报）"),
        lambda s: s.replace('title="多云"', 'title=""'),
        lambda s: s.replace('<li class="sky skyid lv2">', '<li class="other">', 1),
        lambda s: "<html>访问验证</html>",
    ],
)
def test_invalid_forecasts_fail_closed(mutate):
    with pytest.raises(ValueError):
        parse_daily(mutate(page()), "天河", NOW)


def test_missing_future_temp_is_error():
    with pytest.raises(ValueError):
        parse_daily(
            page().replace("<span>30℃</span>", "<span>999℃</span>"), "天河", NOW
        )


def test_query_does_not_call_missing_probability_zero():
    rows = parse_daily(page(), "天河", NOW)
    text = describe_forecast("天河", rows)
    assert "未提供降雨概率" in text
    assert "不代表0%" in text
    assert "降雨概率0%" not in text


def test_temperature_threshold_and_missing_values():
    today = DayRow("2026-12-31", 30, 22, "晴", "晴")
    tomorrow = DayRow("2027-01-01", 27, 22, "晴", "晴")
    verdict = evaluate(today, tomorrow, 3)
    assert verdict.temp_hit and not verdict.precip_hit
    assert verdict.temp_direction == "down"
    assert "添衣" in build_reminder_content("天河", today, tomorrow, verdict)
    assert not evaluate(today, DayRow("2027-01-01"), 3).triggered
    assert not evaluate(None, None, 3).triggered
