"""小回·天气关怀（astrbot_plugin_xiaohui_weather_care）。

两部分能力：
1. 按需天气工具：注册 `get_weather` LLM 工具，用户问天气时由小回自然调用。
2. 前一天条件提醒：每天到点检查"明天 vs 今天"的温差与雨雪预报，
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
from astrbot.core.provider.entities import ProviderRequest

from .logic import (
    DayRow,
    build_reminder_content,
    build_url,
    describe_forecast,
    evaluate,
    parse_daily,
    parse_noon_probability,
    temperature_context,
    resolve_weather_location,
)

PLUGIN_NAME = "astrbot_plugin_xiaohui_weather_care"
DEFAULT_OWNER_UMO = "qq_napcat_100000002:FriendMessage:100000001"


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
        return self._cfg_str("location_name", "北京")

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
            "[weather_care] 已加载 | source=china_weather location=%s city=%s check=%s temp_delta>=%.1f℃ rain_or_snow=forecast",
            self._location_name,
            self._cfg_str("city_id", "101010100"),
            self._cfg_str("check_time", "21:00"),
            self._cfg_float("temp_delta_threshold", 3.0),
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
        city_id = self._cfg_str("city_id", "101010100")
        url = build_url(city_id)
        timeout = aiohttp.ClientTimeout(total=self._cfg_int("request_timeout", 15))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": "xiaohui-weather-care/0.2", "Referer": "https://www.weather.com.cn/"}) as resp:
                resp.raise_for_status()
                payload = await resp.text()
        if city_id != self._cfg_str("city_id", "101010100"):
            raise ValueError("取数期间地点已切换，本次旧地区预报弃用")
        return parse_daily(payload, self._location_name, datetime.now(self._timezone), days)

    # ==================== 判断 + 决策 ====================

    async def _evaluate_now(self) -> tuple[bool, str, list[DayRow]]:
        """取数并判断；返回 (是否触发, 提醒文案, 原始逐日数据)。"""
        rows = await self._fetch_daily(3)
        if len(rows) < 2:
            return False, "", rows
        self._cache_temperature(rows)
        today, tomorrow = rows[0], rows[1]
        verdict = evaluate(
            today,
            tomorrow,
            self._cfg_float("temp_delta_threshold", 3.0),
        )
        if not verdict.triggered:
            return False, "", rows
        content = build_reminder_content(self._location_name, today, tomorrow, verdict)
        return True, content, rows

    def _cache_temperature(self, rows: list[DayRow]) -> None:
        self._state["temperature_background"] = {
            "city_id": self._cfg_str("city_id", "101010100"),
            "captured_at": datetime.now(self._timezone).isoformat(),
            "rows": [{"date": r.date, "tmax": r.tmax, "tmin": r.tmin} for r in rows],
        }
        self._save_state()

    @filter.on_llm_request()
    async def inject_local_temperature(self, event: AstrMessageEvent, req: ProviderRequest):
        if event.unified_msg_origin != self._owner_umo:
            return
        cached = self._state.get("temperature_background") or {}
        overview = ""
        if cached.get("city_id") == self._cfg_str("city_id", "101010100"):
            overview = temperature_context(cached.get("rows", []), cached.get("captured_at", ""), datetime.now(self._timezone))
        if not overview:
            overview = "当前没有可用的近期冷暖预报，不推定今天冷热。"
        req.system_prompt = (req.system_prompt or "") + (
            f"\n[用户所在地天气背景] 当前天气查询地点：{self._cfg_str('location_label', self._location_name)}。"
            "用户明确说明已经到达或现已身处其他地点时，调用set_weather_location更新查询地点；"
            "未来计划、假设、转述不切换。地点对应关系以本地配置为准。"
            "地点不同的本轮，旧地点天气不代表她所在处；切换后的新概况待取数更新。" + overview +
            "冷暖关心参考当地预报与她当下说的体感、室内环境，不根据月份或季节推断她冷不冷。"
            "这些仅作聊天背景，相关时自然使用，不每轮汇报或反复问冷暖；"
            "普通聊天不主动报具体温度或雨情，她明确问天气、或每日天气提醒时照常按具体预报回答。"
        )

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
                await self._maybe_check_noon()
                await self._maybe_check()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("[weather_care] 定时检查异常", exc_info=True)
            await asyncio.sleep(45)

    async def _fetch_noon_probability(self, now: datetime) -> float:
        timeout = aiohttp.ClientTimeout(total=self._cfg_int("request_timeout", 15))
        key = self._cfg_str("probability_location_key", "101924")
        name = self._cfg_str("probability_location_name", "北京")
        if not key.isdigit():
            raise ValueError("天气地区编号无效")
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"https://platform.weathercn.com/live-data/detail.html?key={key}&region=china") as resp:
                resp.raise_for_status()
                payload = await resp.text()
        if key != self._cfg_str("probability_location_key", "101924"):
            raise ValueError("取数期间地点已切换，本次旧地区概率弃用")
        return parse_noon_probability(payload, now, key, name)

    async def _maybe_check_noon(self) -> None:
        if not self._cfg_bool("enabled", True) or not self._cfg_bool("noon_enabled", True):
            return
        now = datetime.now(self._timezone)
        key = now.date().isoformat()
        if self._state.get("noon_check_date") == key:
            return
        start = self._parse_hhmm("12:00", now)
        if not start <= now < start + timedelta(minutes=30):
            return
        # 失败最多每五分钟重取；错过窗口不补发。
        if time.time() - getattr(self, "_noon_attempt_at", 0) < 300:
            return
        self._noon_attempt_at = time.time()
        try:
            self._cache_temperature(await self._fetch_daily(2))
        except Exception:
            logger.warning("[weather_care] 中午冷暖概况更新失败，带伞检查继续", exc_info=True)
        probability = await self._fetch_noon_probability(now)
        triggered = probability >= 60
        if triggered:
            content = (
                f"华风爱科（中国天气/AccuWeather）预报：今天{self._location_name}白天/夜间较高降水概率为{probability:g}%，"
                "达到60%带伞提醒条件，提醒她：下午上班或要出门的话，记得带上伞。结合当天安排自然表达，不预设她一定要上班或外出。"
                "这是白天和夜间两段概率的较高值，不代表必然下雨；按预报自然表达"
            )
            # 先标记本日尝试，避免投递结果不明或重启时重复提醒。
            self._state["noon_check_date"] = key
            self._state["noon_verdict"] = {"at": now.isoformat(), "probability": probability, "triggered": True, "delivery": "attempting"}
            self._save_state()
            delivered = await self._deliver(content)
            self._state["noon_verdict"]["delivery"] = "queued" if delivered else "failed"
        else:
            self._state["noon_check_date"] = key
            self._state["noon_verdict"] = {"at": now.isoformat(), "probability": probability, "triggered": False}
        self._save_state()

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
            "source": "china_weather",
            "city_id": self._cfg_str("city_id", "101010100"),
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

    @filter.llm_tool(name="set_weather_location")
    async def tool_set_weather_location(self, event: AstrMessageEvent, location: str):
        """用户明确说明已经到达或现在身处另一个地点时，切换天气查询位置。

        只适用于用户本人的当前所在地变化，不能用于未来出行计划、假设、引用或他人位置。
        地点及其地区编号由本地 location_profiles 配置。成功后冷暖背景、12点和21点检查共用新地点。

        Args:
            location(string): 已配置的地点名称或别名。
        """
        if not self._is_owner(event):
            return {"ok": False, "msg": "仅限本人会话更新天气地点"}
        profile = resolve_weather_location(location, self.config.get("location_profiles", {}))
        if profile is None:
            return {"ok": False, "msg": "此地点尚未配置，请先补充location_profiles，不得声称已切换"}
        old = {key: self.config.get(key) for key in profile}
        missing = {key for key in profile if key not in self.config}
        try:
            self.config.update(profile)
            self.config.save_config()
        except Exception:
            for key, value in old.items():
                if key in missing:
                    self.config.pop(key, None)
                else:
                    self.config[key] = value
            logger.warning("[weather_care] 地点保存失败", exc_info=True)
            return {"ok": False, "msg": "天气地点保存失败，仍使用原查询地点"}
        self._state.pop("temperature_background", None)
        self._save_state()
        # 切换立即刷新冷暖概况，失败时不使用旧地点缓存，不触发任何提醒。
        try:
            self._cache_temperature(await self._fetch_daily(2))
            fresh = True
        except Exception:
            fresh = False
            logger.warning("[weather_care] 新地点冷暖概况暂未取到", exc_info=True)
        return {"ok": True, "location": profile["location_label"], "temperature_refreshed": fresh}

    @filter.llm_tool(name="get_weather")
    async def tool_get_weather(self, event: AstrMessageEvent, days: int = 3):
        """查询所在地未来几天的天气（温度、白天和夜间天气），用于回答"要不要带伞、穿多少、明天热不热"。

        Args:
            days(number): 想查几天，1~7。默认 3，含今天。问"明天"时用 2。
        Returns:
            含每天温度及白天、夜间天气的文本；来源不提供降雨概率；失败时返回错误说明。
        """
        if not self._cfg_bool("weather_tool_enabled", True):
            return {"ok": False, "msg": "天气工具已关闭"}
        try:
            rows = await self._fetch_daily(days)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[weather_care] 天气工具取数失败：%s", exc)
            return {"ok": False, "msg": f"天气查询失败：{exc}"}
        return {"ok": True, "source": "中国天气网（中央气象台）", "city_id": self._cfg_str("city_id", "101010100"), "location": self._location_name, "text": describe_forecast(self._location_name, rows)}

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
                f"来源：中国天气网（中央气象台）；位置：{self._location_name}，地区编号：{self._cfg_str('city_id', '101280109')}\n"
                f"检查时间：{self._cfg_str('check_time', '21:00')}（窗口 {self._cfg_int('check_window_minutes', 90)} 分钟）\n"
                f"中午检查：12:00，当天白天/夜间降水概率 ≥60% 提醒带伞（华风爱科，启用={self._cfg_bool('noon_enabled', True)}）\n"
                f"阈值：温差 ≥ {self._cfg_float('temp_delta_threshold', 3.0):.0f}℃ 或 明天白天/夜间预报有雨雪\n"
                f"上次数据源：{last.get('source', '旧版Open-Meteo（历史记录）')}\n"
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
