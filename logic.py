"""中国天气网（中央气象台）逐日预报解析与天气关怀判断；不触网、不发消息。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

SOURCE = "中国天气网（中央气象台）"


def build_url(city_id: str) -> str:
    if not re.fullmatch(r"101[0-9]{6}", city_id):
        raise ValueError("中国天气网地区编号格式不正确")
    return f"https://www.weather.com.cn/weather/{city_id}.shtml"


def _temperature(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and -90 <= number <= 65 else None
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class DayRow:
    date: str
    tmax: float | None = None
    tmin: float | None = None
    day_text: str = "未知"
    night_text: str = "未知"

    @property
    def desc(self) -> str:
        return f"白天{self.day_text}、夜间{self.night_text}"

    @property
    def wet(self) -> bool:
        return any(
            word in text
            for text in (self.day_text, self.night_text)
            for word in ("雨", "雪")
        )

    def summary(self) -> str:
        parts = [self.date, self.desc]
        if self.tmin is not None and self.tmax is not None:
            parts.append(f"{self.tmin:.0f}~{self.tmax:.0f}℃")
        return " ".join(parts)


class _ForecastPage(HTMLParser):
    """只提取七日预报列表、地区标题和完整发布时间，不执行网页脚本。"""

    def __init__(self):
        super().__init__()
        self.title = ""
        self.updated = ""
        self.rows = []
        self.row = None
        self.paragraph = ""
        self.capture = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "title":
            self.capture = "title"
        if tag == "input" and attrs.get("id") == "fc_24h_internal_update_time":
            self.updated = attrs.get("value", "")
        if tag == "li" and "sky" in classes:
            if self.row is not None:
                raise ValueError("七日预报列表嵌套异常")
            self.row = {
                "day": "",
                "high": "",
                "low": "",
                "weather": "",
                "has_day": False,
            }
        if self.row is None:
            return
        if tag == "big" and any(re.fullmatch(r"d[0-9]{2}", c) for c in classes):
            self.row["has_day"] = True
        if tag == "h1":
            self.capture = "day"
        if tag == "p":
            self.paragraph = "tem" if "tem" in classes else ""
            if "wea" in classes:
                self.row["weather"] = attrs.get("title", "").strip()
        if self.paragraph == "tem" and tag in ("span", "i"):
            self.capture = "high" if tag == "span" else "low"

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self.capture == "title":
            self.title += data
        elif self.row is not None and self.capture:
            self.row[self.capture] += data

    def handle_endtag(self, tag):
        if tag in ("title", "h1", "span", "i"):
            self.capture = ""
        if tag == "p":
            self.paragraph = ""
        if tag == "li" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def parse_daily(
    payload: str, location: str, now: datetime, days: int = 3
) -> list[DayRow]:
    """校验地区、发布时间及逐日日期；缺少概率时不猜测百分比。"""
    if not isinstance(payload, str):
        raise ValueError("中国天气网未返回有效网页")
    page = _ForecastPage()
    page.feed(payload)
    if f"{location}天气" not in page.title:
        raise ValueError("中国天气网返回地区不匹配或页面不可用")
    now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    try:
        updated = datetime.strptime(page.updated, "%Y%m%d%H").replace(tzinfo=now.tzinfo)
    except (ValueError, TypeError) as exc:
        raise ValueError("中国天气网预报缺少有效发布时间") from exc
    if not -timedelta(minutes=15) <= now - updated <= timedelta(hours=24):
        raise ValueError("中国天气网预报发布时间异常或已过期")
    if len(page.rows) != 7:
        raise ValueError("中国天气网七日预报列表不完整")
    rows = {}
    for offset, item in enumerate(page.rows):
        date = updated.date() + timedelta(days=offset)
        if not re.match(rf"^{date.day}日[（(]", item["day"].strip()):
            raise ValueError("中国天气网逐日日期与发布时间不一致")
        weather = item["weather"]
        if not weather:
            raise ValueError("中国天气网缺少天气现象")
        parts = weather.split("转", 1)
        day_text, night_text = (parts[0], parts[-1])
        if not item["has_day"]:
            day_text = "未知（白天预报已结束）"
        high = _temperature(item["high"].replace("℃", "").strip())
        low = _temperature(item["low"].replace("℃", "").strip())
        if date > now.date() and (high is None or low is None or not item["has_day"]):
            raise ValueError("中国天气网未来预报字段不完整")
        rows[date] = DayRow(date.isoformat(), high, low, day_text, night_text)
    dates = [now.date() + timedelta(days=i) for i in range(max(1, min(7, int(days))))]
    if any(date not in rows for date in dates):
        raise ValueError("中国天气网预报未覆盖请求日期")
    return [rows[date] for date in dates]


@dataclass(frozen=True)
class Verdict:
    triggered: bool
    reasons: tuple[str, ...]
    temp_hit: bool
    precip_hit: bool
    temp_direction: str
    max_delta: float | None


def evaluate(
    today: DayRow | None, tomorrow: DayRow | None, temp_delta_threshold: float
) -> Verdict:
    reasons: list[str] = []
    direction = ""
    max_delta = None
    if today is not None and tomorrow is not None:
        for label, a, b in (
            ("最高温", today.tmax, tomorrow.tmax),
            ("最低温", today.tmin, tomorrow.tmin),
        ):
            if a is None or b is None:
                continue
            delta = b - a
            if abs(delta) >= temp_delta_threshold:
                reasons.append(f"{label}{'升' if delta > 0 else '降'}{abs(delta):.0f}℃")
                if max_delta is None or abs(delta) > max_delta:
                    max_delta = abs(delta)
                    direction = "up" if delta > 0 else "down"
    temp_hit = bool(reasons)
    precip_hit = tomorrow is not None and tomorrow.wet
    if precip_hit:
        reasons.append(tomorrow.desc)
    return Verdict(
        bool(reasons), tuple(reasons), temp_hit, precip_hit, direction, max_delta
    )


def build_reminder_content(
    location: str, today: DayRow | None, tomorrow: DayRow | None, verdict: Verdict
) -> str:
    if tomorrow is None or not verdict.triggered:
        return ""
    facts = [f"{SOURCE}预报：明天{location or '所在地'}{tomorrow.desc}"]
    if tomorrow.tmin is not None and tomorrow.tmax is not None:
        facts.append(f"{tomorrow.tmin:.0f}～{tomorrow.tmax:.0f}℃")
    if verdict.temp_hit:
        facts.append(
            "气温明显下降" if verdict.temp_direction == "down" else "气温明显上升"
        )
    advice = []
    if verdict.precip_hit:
        advice.append("出门带伞")
    if verdict.temp_hit:
        advice.append(
            "注意添衣保暖" if verdict.temp_direction == "down" else "穿着注意散热"
        )
    return "，".join(
        facts
        + [
            "提醒她" + "、".join(advice),
            "按预报表达，天气可能变化；该来源不提供降雨概率，不推算百分比",
        ]
    )


def describe_forecast(location: str, rows: list[DayRow]) -> str:
    lines = [f"{location or '所在地'}未来天气预报（来源：{SOURCE}）："]
    lines.extend(f"- {row.summary()}" for row in rows)
    lines.append(
        "此来源未提供降雨概率，不代表0%；只按上述天气现象表达，天气预报可能变化。"
    )
    return "\n".join(lines)


class _ProbabilityPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocks = {}
        self.active = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("type") == "application/json":
            self.active = attrs.get("id")
            if self.active in self.blocks:
                raise ValueError("天气页面数据块重复")
            self.blocks[self.active] = ""

    def handle_data(self, data):
        if self.active:
            self.blocks[self.active] += data

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = None


def parse_noon_probability(payload: str, now: datetime, location_key: str = "101924", location_name: str = "北京") -> float:
    """读取华风爱科当前地区当天日/夜概率；地区、日期或概率缺失则拒绝。"""
    import json

    page = _ProbabilityPage()
    page.feed(payload)
    try:
        city = json.loads(page.blocks["wx-city-data"])
        rows = json.loads(page.blocks["wx-daily-forecast-data"])
        if city.get("location_key") != location_key or city.get("name_zh") != location_name or city.get("region") != "china":
            raise ValueError("天气页面地区不匹配")
        today = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
        matches = [row for row in rows if datetime.fromisoformat(row["Date"]).astimezone(ZoneInfo("Asia/Shanghai")).date() == today]
        if len(matches) != 1:
            raise ValueError("天气页面缺少当天唯一预报")
        row = matches[0]
        stamp = datetime.fromisoformat(row["Date"])
        if stamp.utcoffset() != timedelta(hours=8):
            raise ValueError("天气预报时区不匹配")
        values = [row[period]["PrecipitationProbability"] for period in ("Day", "Night")]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 100 for v in values):
            raise ValueError("天气页面降水概率无效")
        return float(max(values))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("天气页面缺少有效概率数据") from exc


def temperature_context(rows: list[dict], captured_at: str, now: datetime) -> str:
    """只使用新近预报中当天的温度概况，不暴露数值或雨情。"""
    try:
        stamp = datetime.fromisoformat(captured_at)
        if stamp.tzinfo is None or not -timedelta(minutes=5) <= now - stamp <= timedelta(hours=30):
            return ""
        today = now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        matches = [r for r in rows if r.get("date") == today]
        if len(matches) != 1:
            return ""
        row = matches[0]
        high, low = _temperature(row.get("tmax")), _temperature(row.get("tmin"))
        if high is None or low is None or high < low:
            return ""
        daytime = "偏热" if high >= 26 else "温和" if high >= 22 else "偏凉" if high >= 16 else "偏冷"
        nighttime = "偏暖" if low >= 22 else "较凉爽" if low >= 18 else "偏凉" if low >= 12 else "偏冷"
        return f"最近一次当地预报对今天的概况：白天{daytime}，早晚{nighttime}。这是预报概况，不代表她此刻实际体感。"
    except (TypeError, ValueError, AttributeError):
        return ""



def resolve_weather_location(location: str, profiles: dict | None = None) -> dict | None:
    """Resolve a locally configured exact location name or alias."""
    profiles = profiles or {}
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        if location.strip() == name or location.strip() in profile.get("aliases", []):
            required = ("location_name", "location_label", "city_id", "probability_location_key", "probability_location_name")
            if all(isinstance(profile.get(key), str) and profile[key].strip() for key in required):
                return {key: profile[key] for key in required}
    return None
