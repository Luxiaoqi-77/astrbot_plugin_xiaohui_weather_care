"""纯逻辑离线测试：不触网、不依赖 AstrBot 上下文。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logic import (  # noqa: E402
    DayRow,
    build_reminder_content,
    build_url,
    describe_forecast,
    evaluate,
    parse_daily,
)

SAMPLE_PAYLOAD = {
    "latitude": 39.9042,
    "longitude": 116.4074,
    "timezone": "Asia/Shanghai",
    "daily_units": {"temperature_2m_max": "°C"},
    "daily": {
        "time": ["2026-10-16", "2026-10-17", "2026-10-18"],
        "weather_code": [3, 53, 51],
        "temperature_2m_max": [32.8, 32.9, 31.7],
        "temperature_2m_min": [23.3, 24.0, 24.5],
        "precipitation_probability_max": [0, 39, 72],
    },
}


def test_build_url_basic():
    url = build_url(39.9042, 116.4074, "Asia/Shanghai", 3)
    assert "latitude=39.9042" in url
    assert "longitude=116.4074" in url
    assert "forecast_days=3" in url
    assert "precipitation_probability_max" in url


def test_build_url_clamps_days():
    assert "forecast_days=1" in build_url(1, 2, "Asia/Shanghai", 0)
    assert "forecast_days=7" in build_url(1, 2, "Asia/Shanghai", 99)


def test_parse_daily_real_payload():
    rows = parse_daily(SAMPLE_PAYLOAD)
    assert [r.date for r in rows] == ["2026-10-16", "2026-10-17", "2026-10-18"]
    assert rows[0].tmax == 32.8
    assert rows[0].tmin == 23.3
    assert rows[0].precip_prob == 0
    assert rows[1].desc == "毛毛雨"
    assert rows[2].desc == "毛毛雨"


def test_parse_daily_from_json_string():
    import json

    rows = parse_daily(json.dumps(SAMPLE_PAYLOAD))
    assert len(rows) == 3


def test_parse_daily_bad_payload():
    assert parse_daily(None) == []
    assert parse_daily("not json") == []
    assert parse_daily({}) == []
    assert parse_daily({"daily": {"time": []}}) == []


def test_parse_daily_missing_columns_keeps_none():
    payload = {
        "daily": {
            "time": ["2026-10-16", "2026-10-17"],
            "temperature_2m_max": [30.0],
            "temperature_2m_min": [20.0],
            "precipitation_probability_max": [10],
            "weather_code": [0],
        }
    }
    rows = parse_daily(payload)
    assert rows[0].tmax == 30.0
    # 第二天缺列 → None，不猜
    assert rows[1].tmax is None
    assert rows[1].precip_prob is None


def _row(date="2026-10-17", tmax=30.0, tmin=20.0, prob=10, code=0):
    return DayRow(date=date, code=code, tmax=tmax, tmin=tmin, precip_prob=prob)


def test_evaluate_not_triggered_when_flat():
    today = _row(date="2026-10-16", tmax=32.8, tmin=23.3, prob=0)
    tomorrow = _row(date="2026-10-17", tmax=32.9, tmin=24.0, prob=39)
    verdict = evaluate(today, tomorrow, 3.0, 80)
    assert verdict.triggered is False
    assert verdict.temp_hit is False
    assert verdict.precip_hit is False


def test_evaluate_triggers_on_cooldown():
    today = _row(date="2026-10-16", tmax=32.0, tmin=24.0, prob=0)
    tomorrow = _row(date="2026-10-17", tmax=28.0, tmin=23.0, prob=10)
    verdict = evaluate(today, tomorrow, 3.0, 80)
    assert verdict.triggered is True
    assert verdict.temp_hit is True
    assert verdict.temp_direction == "down"
    assert verdict.max_delta == 4.0


def test_evaluate_triggers_on_warming():
    today = _row(date="2026-10-16", tmax=30.0, tmin=20.0, prob=0)
    tomorrow = _row(date="2026-10-17", tmax=33.0, tmin=20.5, prob=10)
    verdict = evaluate(today, tomorrow, 3.0, 80)
    assert verdict.triggered is True
    assert verdict.temp_direction == "up"


def test_evaluate_boundary_exactly_threshold():
    # 恰好等于阈值 → 命中（>=）
    today = _row(date="2026-10-16", tmax=30.0, tmin=20.0, prob=0)
    tomorrow = _row(date="2026-10-17", tmax=33.0, tmin=20.0, prob=0)
    assert evaluate(today, tomorrow, 3.0, 80).triggered is True


def test_evaluate_just_below_threshold():
    today = _row(date="2026-10-16", tmax=30.0, tmin=20.0, prob=0)
    tomorrow = _row(date="2026-10-17", tmax=32.9, tmin=20.0, prob=0)
    assert evaluate(today, tomorrow, 3.0, 80).triggered is False


def test_evaluate_triggers_on_rain_probability():
    today = _row(date="2026-10-16", tmax=30.0, tmin=20.0, prob=0)
    tomorrow = _row(date="2026-10-17", tmax=30.0, tmin=20.0, prob=85, code=63)
    verdict = evaluate(today, tomorrow, 3.0, 80)
    assert verdict.triggered is True
    assert verdict.temp_hit is False
    assert verdict.precip_hit is True


def test_evaluate_precip_boundary_strict():
    today = _row(date="2026-10-16", tmax=30.0, tmin=20.0, prob=0)
    # 恰好 80 → 不命中（严格大于）
    tomorrow = _row(date="2026-10-17", tmax=30.0, tmin=20.0, prob=80)
    assert evaluate(today, tomorrow, 3.0, 80).triggered is False
    # 81 → 命中
    tomorrow2 = _row(date="2026-10-17", tmax=30.0, tmin=20.0, prob=81)
    assert evaluate(today, tomorrow2, 3.0, 80).triggered is True


def test_evaluate_missing_data_never_triggers():
    today = DayRow(date="2026-10-16")
    tomorrow = DayRow(date="2026-10-17")
    verdict = evaluate(today, tomorrow, 3.0, 80)
    assert verdict.triggered is False
    assert evaluate(None, tomorrow, 3.0, 80).triggered is False
    assert evaluate(today, None, 3.0, 80).triggered is False


def test_build_reminder_content_cooldown_and_rain():
    today = _row(date="2026-10-16", tmax=32.0, tmin=24.0, prob=0)
    tomorrow = _row(date="2026-10-17", tmax=28.0, tmin=22.0, prob=90, code=63)
    verdict = evaluate(today, tomorrow, 3.0, 80)
    content = build_reminder_content("示例城市", today, tomorrow, verdict)
    assert "示例城市" in content
    assert "降温" in content
    assert "带伞" in content


def test_build_reminder_content_warming():
    today = _row(date="2026-10-16", tmax=28.0, tmin=20.0, prob=0)
    tomorrow = _row(date="2026-10-17", tmax=32.0, tmin=20.0, prob=0)
    verdict = evaluate(today, tomorrow, 3.0, 80)
    content = build_reminder_content("示例城市", today, tomorrow, verdict)
    assert "升温" in content


def test_build_reminder_content_empty_when_not_triggered():
    today = _row(date="2026-10-16", tmax=30.0, tmin=20.0, prob=0)
    tomorrow = _row(date="2026-10-17", tmax=30.0, tmin=20.0, prob=10)
    verdict = evaluate(today, tomorrow, 3.0, 80)
    assert build_reminder_content("示例城市", today, tomorrow, verdict) == ""


def test_describe_forecast():
    rows = parse_daily(SAMPLE_PAYLOAD)
    text = describe_forecast("示例城市", rows)
    assert "示例城市" in text
    assert "2026-10-17" in text
    assert "降雨概率39%" in text
    assert describe_forecast("示例城市", []) != ""
