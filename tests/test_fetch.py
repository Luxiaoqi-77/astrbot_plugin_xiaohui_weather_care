"""运行生产类取数/试算入口，用模拟HTTP验证，不加载AstrBot或发送提醒。"""

import ast
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import aiohttp
import pytest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import logic
from test_logic import NOW, page


def production_class():
    # 只剥离AstrBot注册装饰器及基类，保留真实方法体；构造时不执行插件初始化。
    path = Path(__file__).resolve().parents[1] / "main.py"
    source = ast.parse(path.read_text())
    cls = next(n for n in source.body if isinstance(n, ast.ClassDef))
    cls.decorator_list = []
    cls.bases = [ast.Name(id="object", ctx=ast.Load())]
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.decorator_list = [
                d
                for d in node.decorator_list
                if isinstance(d, ast.Name) and d.id == "property"
            ]
    namespace = {
        **vars(logic),
        "aiohttp": aiohttp,
        "datetime": datetime,
        "timedelta": timedelta,
        "ZoneInfo": ZoneInfo,
        "Optional": Optional,
        "asyncio": asyncio,
    }
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            cls,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[cls.name]


@pytest.mark.parametrize(
    "failure,weather", [(False, "多云"), (False, "多云转小雨"), (True, "多云")]
)
def test_production_fetch_and_evaluate(monkeypatch, failure, weather):
    cls = production_class()
    plugin = cls.__new__(cls)
    plugin._state = {}
    plugin._save_state = lambda: None
    plugin.config = {
        "location_name": "天河",
        "city_id": "101280109",
        "timezone": "Asia/Shanghai",
        "temp_delta_threshold": 3,
    }
    calls = []

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            if failure:
                raise RuntimeError("HTTP failed")

        async def text(self):
            return page(weather)

    class Session(Response):
        def __init__(self, **kwargs):
            pass

        def get(self, url, headers):
            calls.append(url)
            assert headers["Referer"] == "https://www.weather.com.cn/"
            return Response()

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    monkeypatch.setitem(
        cls._fetch_daily.__globals__, "datetime", SimpleNamespace(now=lambda tz: NOW)
    )
    if failure:
        with pytest.raises(RuntimeError, match="HTTP failed"):
            asyncio.run(plugin._evaluate_now())
    else:
        triggered, content, rows = asyncio.run(plugin._evaluate_now())
        assert triggered == (weather == "多云转小雨")
        if triggered:
            assert "带伞" in content and "夜间小雨" in content and "%" not in content
        else:
            assert content == "" and rows[1].night_text == "多云"
    assert calls == ["https://www.weather.com.cn/weather/101280109.shtml"]
