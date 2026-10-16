"""小回·天气关怀 —— 纯逻辑层。

只做两件事，全部是无副作用的纯函数，方便离线测试：
1. 组装 Open-Meteo 请求 URL / 解析逐日预报；
2. 按"温差或降雨概率"规则判断是否值得提醒，并生成给提醒插件的自然语言素材。

不在此文件里做任何网络请求、不碰 AstrBot 上下文。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

DAILY_FIELDS = (
    "weather_code",
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_probability_max",
)

# WMO weather code → 中文短语（够用即可，未知码回落为"未知"）
WMO_DESC: dict[int, str] = {
    0: "晴",
    1: "大部晴朗",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "毛毛雨",
    53: "毛毛雨",
    55: "毛毛雨",
    56: "冻毛毛雨",
    57: "冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴冰雹",
    99: "雷阵雨伴冰雹",
}


@dataclass(frozen=True)
class DayRow:
    """一天的结构化预报。字段缺失时保留 None，绝不猜数。"""

    date: str
    code: Optional[int] = None
    tmax: Optional[float] = None
    tmin: Optional[float] = None
    precip_prob: Optional[int] = None

    @property
    def desc(self) -> str:
        if self.code is None:
            return "未知"
        return WMO_DESC.get(int(self.code), "未知")

    def summary(self) -> str:
        """给模型看的一行摘要，例如「10-17 阴 24~33℃ 降雨概率39%」。"""
        parts = [self.date, self.desc]
        if self.tmin is not None and self.tmax is not None:
            parts.append(f"{self.tmin:.0f}~{self.tmax:.0f}℃")
        if self.precip_prob is not None:
            parts.append(f"降雨概率{self.precip_prob}%")
        return " ".join(parts)


def build_url(latitude: float, longitude: float, timezone: str, days: int) -> str:
    """组装 Open-Meteo 逐日预报 URL。days 会被夹到 1~7。"""
    safe_days = max(1, min(7, int(days)))
    tz = timezone or "Asia/Shanghai"
    return (
        f"{OPEN_METEO_URL}"
        f"?latitude={float(latitude)}"
        f"&longitude={float(longitude)}"
        f"&daily={','.join(DAILY_FIELDS)}"
        f"&timezone={tz}"
        f"&forecast_days={safe_days}"
    )


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    num = _as_float(value)
    return None if num is None else int(round(num))


def parse_daily(payload: Any, expected_days: int = 3) -> list[DayRow]:
    """把 Open-Meteo 的 JSON 解析成 DayRow 列表；结构不对就返回空列表。

    Args:
        payload: 响应对象（dict）或 JSON 字符串。
        expected_days: 期望天数，预留给调用方，不强制截断。

    Returns:
        DayRow 列表，按响应里的日期顺序（Open-Meteo 为升序）。
    """
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return []
    if not isinstance(payload, dict):
        return []

    daily = payload.get("daily")
    if not isinstance(daily, dict):
        return []

    dates = daily.get("time")
    if not isinstance(dates, list) or not dates:
        return []

    def _col(name: str) -> list[Any]:
        col = daily.get(name)
        return col if isinstance(col, list) else []

    codes = _col("weather_code")
    tmaxs = _col("temperature_2m_max")
    tmins = _col("temperature_2m_min")
    probs = _col("precipitation_probability_max")

    rows: list[DayRow] = []
    for idx, date in enumerate(dates):
        if not isinstance(date, str):
            continue

        def _pick(col: list[Any]) -> Any:
            return col[idx] if idx < len(col) else None

        rows.append(
            DayRow(
                date=date,
                code=_as_int(_pick(codes)),
                tmax=_as_float(_pick(tmaxs)),
                tmin=_as_float(_pick(tmins)),
                precip_prob=_as_int(_pick(probs)),
            )
        )
    return rows


@dataclass(frozen=True)
class Verdict:
    """一次判断的结果。"""

    triggered: bool
    reasons: tuple[str, ...]  # 命中原因（中文短语，便于日志）
    temp_hit: bool
    precip_hit: bool
    temp_direction: str  # "up" / "down" / ""
    max_delta: Optional[float]  # 命中的温差绝对值最大值


def evaluate(
    today: Optional[DayRow],
    tomorrow: Optional[DayRow],
    temp_delta_threshold: float,
    precip_probability_threshold: int,
) -> Verdict:
    """判断"明天"是否值得提醒。

    命中条件（任一）：
      1. |明天最高温 - 今天最高温| >= 阈值；
      2. |明天最低温 - 今天最低温| >= 阈值；
      3. 明天降雨概率 > 阈值（严格大于）。

    Args:
        today: 今天（可能缺字段）。
        tomorrow: 明天（可能缺字段）。
        temp_delta_threshold: 温差阈值（摄氏度）。
        precip_probability_threshold: 降雨概率阈值（百分比，严格大于）。

    Returns:
        Verdict。数据不足时不触发。
    """
    reasons: list[str] = []
    direction = ""
    max_delta: Optional[float] = None
    temp_hit = False

    if today is not None and tomorrow is not None:
        for label, a, b in (
            ("最高温", today.tmax, tomorrow.tmax),
            ("最低温", today.tmin, tomorrow.tmin),
        ):
            if a is None or b is None:
                continue
            delta = float(b) - float(a)
            if abs(delta) >= float(temp_delta_threshold):
                temp_hit = True
                direction = "up" if delta > 0 else "down"
                sign = "升" if delta > 0 else "降"
                reasons.append(f"{label}{sign}{abs(delta):.0f}℃")
                if max_delta is None or abs(delta) > max_delta:
                    max_delta = abs(delta)

    precip_hit = bool(
        tomorrow is not None
        and tomorrow.precip_prob is not None
        and int(tomorrow.precip_prob) > int(precip_probability_threshold)
    )
    if precip_hit:
        reasons.append(f"降雨概率{tomorrow.precip_prob}%")  # type: ignore[union-attr]

    return Verdict(
        triggered=bool(reasons),
        reasons=tuple(reasons),
        temp_hit=temp_hit,
        precip_hit=precip_hit,
        temp_direction=direction,
        max_delta=max_delta,
    )


def build_reminder_content(
    location: str,
    today: Optional[DayRow],
    tomorrow: Optional[DayRow],
    verdict: Verdict,
) -> str:
    """生成交给提醒插件的"待提醒事项"文案（事实为主，附带一句建议）。"""
    if tomorrow is None or not verdict.triggered:
        return ""

    loc = location or "所在地"
    facts: list[str] = []

    if verdict.temp_hit:
        verb = "升温" if verdict.temp_direction == "up" else "降温"
        parts: list[str] = []
        if tomorrow.tmax is not None:
            parts.append(f"最高{tomorrow.tmax:.0f}℃")
        if tomorrow.tmin is not None:
            parts.append(f"最低{tomorrow.tmin:.0f}℃")
        temp_range = "、".join(parts)
        suffix = f"（{temp_range}）" if temp_range else ""
        facts.append(f"明天{loc}会明显{verb}{suffix}")

    if verdict.precip_hit and tomorrow.precip_prob is not None:
        facts.append(f"明天{loc}有雨（降雨概率{tomorrow.precip_prob}%）")

    if not facts:
        return ""

    advice: list[str] = []
    if verdict.precip_hit:
        advice.append("记得带伞")
    if verdict.temp_hit:
        advice.append("注意添衣保暖" if verdict.temp_direction == "down" else "别穿太多热着")
    advice_text = "，".join(advice)
    tail = f"，提醒她{advice_text}" if advice_text else ""
    return "；".join(facts) + tail


def describe_forecast(location: str, rows: list[DayRow]) -> str:
    """把多天预报拼成给模型看的文本（用于按需查询工具）。"""
    loc = location or "所在地"
    if not rows:
        return f"暂时取不到{loc}的天气数据。"
    lines = [f"{loc}未来天气预报："]
    lines.extend(f"- {row.summary()}" for row in rows)
    return "\n".join(lines)
