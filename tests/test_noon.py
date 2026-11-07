import asyncio
import json
import logging
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from test_fetch import production_class
from logic import parse_noon_probability

NOW = datetime(2026, 11, 7, 12, tzinfo=ZoneInfo('Asia/Shanghai'))


def page(day=60, night=20, date='2026-11-07', key='101924'):
    city = {'location_key': key, 'name_zh': '北京', 'region': 'china'}
    rows = [{'Date': date+'T11:00:00+08:00', 'Day': {'PrecipitationProbability': day}, 'Night': {'PrecipitationProbability': night}}]
    return ''.join(f'<script type="application/json" id="{key}">{json.dumps(value)}</script>' for key, value in [('wx-city-data', city), ('wx-daily-forecast-data', rows)])


@pytest.mark.parametrize('day,night,expected', [(0,0,0),(59,20,59),(60,20,60),(20,70,70),(100,20,100)])
def test_probability(day, night, expected):
    assert parse_noon_probability(page(day,night), NOW) == expected


@pytest.mark.parametrize('kwargs', [{'day':None},{'day':-1},{'night':101},{'day':True},{'day':float('nan')},{'date':'2026-11-06'},{'key':'102255'}])
def test_reject_invalid(kwargs):
    with pytest.raises(ValueError):
        parse_noon_probability(page(**kwargs), NOW)


@pytest.mark.parametrize('hour,probability,enabled,expected', [(11,80,True,0),(12,59,True,0),(12,60,True,1),(12,90,False,0),(13,80,True,0)])
def test_noon_schedule_and_dedup(monkeypatch, hour, probability, enabled, expected):
    cls = production_class()
    plugin = cls.__new__(cls)
    plugin.config = {'noon_enabled':enabled}
    plugin._state = {}
    namespace = cls._maybe_check_noon.__globals__
    monkeypatch.setitem(namespace, 'datetime', SimpleNamespace(now=lambda tz: NOW.replace(hour=hour)))
    monkeypatch.setitem(namespace, 'time', time)
    monkeypatch.setitem(namespace, 'logger', logging.getLogger('test'))
    plugin._save_state = lambda: None
    delivered = []
    async def fetch(now): return probability
    async def deliver(content): delivered.append(content); return True
    async def daily(days): return []
    plugin._fetch_daily = daily
    plugin._fetch_noon_probability = fetch
    plugin._deliver = deliver
    async def run():
        await plugin._maybe_check_noon()
        await plugin._maybe_check_noon()
    asyncio.run(run())
    assert len(delivered) == expected
    if expected: assert '60%' in delivered[0] and '华风爱科' in delivered[0]
