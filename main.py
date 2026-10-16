"""小回·天气关怀（astrbot_plugin_xiaohui_weather_care）。

两部分能力：
1. 按需天气工具：注册 `get_weather` LLM 工具，用户问天气时由小回自然调用。
2. 前一天条件提醒：每天到点检查"明天 vs 今天"的温差与降雨概率，
   **只有命中阈值**时才向 `xavier_reminder` 落一条"即时提醒"，
   由它走完整的伪造消息注入管线（因此自动继承意识同步拦截、与 wakeup 的
   合并窗口、防抖等既有护栏）；不命中则什么都不做。

设计红线：
- 本插件**不自己发消息**、不自己调 LLM、不自己写历史，只负责取数与判断。
- 判断全部在代码里做，不依赖模型算术。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .logic import (
    DayRow,
    build_reminder_content,
    build_url,
    describe_forecast,
    evaluate,
    parse_daily,
)

PLUGIN_NAME = "astrbot_plugin_xiaohui_weather_care"
DEFAULT_OWNER_UMO = "qq_napcat_2258780898:FriendMessage:2870791457"


@register(
    PLUGIN_NAME,
    "local",
    "小回·天气关怀：按需天气工具 + 前一天条件提醒（温差/降雨）",
    "0.1.0",
)
class XiaoHuiWeatherCare(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        try:
            self.data_dir = str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            self.data_dir = os.path.dirname(os.path.abspath(__file__))
        os.makedirs(self.data_dir, exist_ok=True)
        self.state_path = os.path.join(self.data_dir, "state.json")
        self._state: dict = self._load_state()

        self._task: Optional[asyncio.Task] = None
        self._stop = False

    # ==================== 配置读取 ====================

    def _cfg_bool(self, key: str, default: bool) -> bool:
        return bool(self.config.get(key, default))

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_str(self, key: str, default: str) -> str:
        value = self.config.get(key, default)
        return str(value) if value is not None and str(value).strip() != "" else default

    @property
    def _timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self._cfg_str("timezone", "Asia/Shanghai"))
        except Exception:
            return ZoneInfo("Asia/Shanghai")

    @property
    def _owner_umo(self) -> str:
        return self._cfg_str("owner_umo", DEFAULT_OWNER_UMO)

    @property
    def _location_name(self) -> str:
        return self._cfg_str("location_name", "示例城市")

    # ==================== 状态持久化 ====================

    def _load_state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as fp:
                data = json.load(fp)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self._state, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
        except OSError:
            logger.warning("[weather_care] 状态写入失败", exc_info=True)

    # ==================== 生命周期 ====================

    async def initialize(self) -> None:
        if self._task is None:
            self._stop = False
            self._task = asyncio.create_task(self._loop())
        logger.info(
            "[weather_care] 已加载 | location=%s lat=%.4f lon=%.4f check=%s temp_delta>=%.1f℃ precip>%d%%",
            self._location_name,
            self._cfg_float("latitude", 39.9042),
            self._cfg_float("longitude", 116.4074),
            self._cfg_str("check_time", "21:00"),
            self._cfg_float("temp_delta_threshold", 3.0),
            self._cfg_int("precip_probability_threshold", 70),
        )

    async def terminate(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # ==================== 取数 ====================

    async def _fetch_daily(self, days: int) -> list[DayRow]:
        lat = self._cfg_float("latitude", 39.9042)
        lon = self._cfg_float("longitude", 116.4074)
        url = build_url(lat, lon, self._cfg_str("timezone", "Asia/Shanghai"), days)
        timeout = aiohttp.ClientTimeout(total=self._cfg_int("request_timeout", 15))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": "xiaohui-weather-care/0.1"}) as resp:
                resp.raise_for_status()
                payload = await resp.json()
        return parse_daily(payload, expected_days=days)

    # ==================== 判断 + 决策 ====================

    async def _evaluate_now(self) -> tuple[bool, str, list[DayRow]]:
        """取数并判断；返回 (是否触发, 提醒文案, 原始逐日数据)。"""
        rows = await self._fetch_daily(3)
        if len(rows) < 2:
            return False, "", rows
        today, tomorrow = rows[0], rows[1]
        verdict = evaluate(
            today,
            tomorrow,
            self._cfg_float("temp_delta_threshold", 3.0),
            self._cfg_int("precip_probability_threshold", 80),
        )
        if not verdict.triggered:
            return False, "", rows
        content = build_reminder_content(self._location_name, today, tomorrow, verdict)
        return True, content, rows

    def _build_reminder(self, content: str):
        """构造一条即时提醒对象；返回 (Reminder 或 None, 错误说明)。

        只复用 xavier_reminder 的存储与调度，不复制它的注入逻辑 ——
        这样意识同步拦截、wakeup 合并窗口、防抖等护栏全部自动继承。
        """
        try:
            metadata = self.context.get_registered_star("xavier_reminder")
        except Exception:
            metadata = None
        plugin = getattr(metadata, "star_cls", None) if metadata is not None else None
        if metadata is None or not getattr(metadata, "activated", False) or plugin is None:
            return None, "xavier_reminder 未加载或未激活"
        store = getattr(plugin, "store", None)
        if store is None:
            return None, "xavier_reminder 存储不可用"
        try:
            store_mod = importlib.import_module(type(store).__module__)
            reminder_cls = getattr(store_mod, "Reminder")
        except Exception:
            return None, "无法获取 Reminder 类型"

        reminder = reminder_cls(
            umo=self._owner_umo,
            type="once",
            content=content,
            next_fire_ts=time.time() + 3.0,
            created_by="weather_care",
        )
        return reminder, ""

    async def _deliver(self, content: str) -> bool:
        reminder, error = self._build_reminder(content)
        if reminder is None:
            logger.warning("[weather_care] 提醒投递失败：%s", error)
            return False
        try:
            plugin = self.context.get_registered_star("xavier_reminder").star_cls
            await plugin.store.add(reminder)
        except Exception:
            logger.warning("[weather_care] 写入提醒存储失败", exc_info=True)
            return False
        logger.info("[weather_care] 已投递天气关怀提醒 | umo=%s", self._owner_umo)
        return True

    # ==================== 定时循环 ====================

    async def _loop(self) -> None:
        while not self._stop:
            try:
                await self._maybe_check()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("[weather_care] 定时检查异常", exc_info=True)
            await asyncio.sleep(45)

    async def _maybe_check(self) -> None:
        if not self._cfg_bool("enabled", True):
            return
        now = datetime.now(self._timezone)
        today_key = now.date().isoformat()
        if self._state.get("last_check_date") == today_key:
            return

        start = self._parse_hhmm(self._cfg_str("check_time", "21:00"), now)
        if start is None:
            return
        window_end = start + timedelta(minutes=max(1, self._cfg_int("check_window_minutes", 90)))
        if not (start <= now <= window_end):
            return

        triggered, content, rows = await self._evaluate_now()
        self._state["last_check_date"] = today_key
        self._state["last_verdict"] = {
            "at": now.isoformat(),
            "triggered": triggered,
            "content": content,
            "days": [r.summary() for r in rows],
        }
        self._save_state()

        if triggered and content:
            await self._deliver(content)
        else:
            logger.info("[weather_care] 今日未达阈值，不提醒 | %s", today_key)

    def _parse_hhmm(self, text: str, now: datetime) -> Optional[datetime]:
        try:
            hour, minute = text.strip().split(":")
            return now.replace(
                hour=int(hour), minute=int(minute), second=0, microsecond=0
            )
        except (ValueError, AttributeError):
            return None

    # ==================== 按需天气工具 ====================

    @filter.llm_tool(name="get_weather")
    async def tool_get_weather(self, event: AstrMessageEvent, days: int = 3):
        """查询所在地未来几天的天气（温度、降雨概率），用于回答"要不要带伞、穿多少、明天热不热"。

        Args:
            days(number): 想查几天，1~7。默认 3，含今天。问"明天"时用 2。
        Returns:
            含每天温度与降雨概率的文本；失败时返回错误说明。
        """
        if not self._cfg_bool("weather_tool_enabled", True):
            return {"ok": False, "msg": "天气工具已关闭"}
        try:
            rows = await self._fetch_daily(days)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[weather_care] 天气工具取数失败：%s", exc)
            return {"ok": False, "msg": f"天气查询失败：{exc}"}
        return {"ok": True, "location": self._location_name, "text": describe_forecast(self._location_name, rows)}

    # ==================== 手动命令（便于验证） ====================

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        return event.unified_msg_origin == self._owner_umo

    @filter.command("天气关怀")
    async def cmd_weather_care(self, event: AstrMessageEvent, action: str = "状态"):
        """天气关怀：/天气关怀 状态|测试|强制发送"""
        if not self._is_owner(event):
            yield event.plain_result("仅限所有者私聊使用。")
            return
        act = (action or "状态").strip()
        if act in ("状态", "status"):
            last = self._state.get("last_verdict") or {}
            yield event.plain_result(
                "【天气关怀】\n"
                f"位置：{self._location_name}（{self._cfg_float('latitude', 0):.4f},{self._cfg_float('longitude', 0):.4f}）\n"
                f"检查时间：{self._cfg_str('check_time', '21:00')}（窗口 {self._cfg_int('check_window_minutes', 90)} 分钟）\n"
                f"阈值：温差 ≥ {self._cfg_float('temp_delta_threshold', 3.0):.0f}℃ 或 降雨概率 > {self._cfg_int('precip_probability_threshold', 80)}%\n"
                f"上次检查：{last.get('at', '（无）')}，触发={last.get('triggered')}\n"
                f"上次结论：{last.get('content') or '—'}"
            )
            return
        if act in ("测试", "test"):
            try:
                triggered, content, rows = await self._evaluate_now()
            except Exception as exc:  # noqa: BLE001
                yield event.plain_result(f"取数失败：{exc}")
                return
            lines = [f"【试算】触发={triggered}"]
            lines += [f"- {r.summary()}" for r in rows]
            if triggered:
                lines.append(f"文案：{content}")
            yield event.plain_result("\n".join(lines))
            return
        if act in ("强制发送", "force"):
            try:
                triggered, content, rows = await self._evaluate_now()
            except Exception as exc:  # noqa: BLE001
                yield event.plain_result(f"取数失败：{exc}")
                return
            if not content:
                content = f"明天{self._location_name}的天气随手看一眼就好"
            delivered = await self._deliver(content)
            yield event.plain_result(f"已投递={delivered}；文案：{content}")
            return
        yield event.plain_result("用法：/天气关怀 状态|测试|强制发送")
