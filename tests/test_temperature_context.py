from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import asyncio

import pytest
from logic import temperature_context
from test_fetch import production_class

NOW = datetime(2026, 11, 7, 14, tzinfo=ZoneInfo('Asia/Shanghai'))


def rows(high=29, low=22, date='2026-11-07'):
    return [{'date':date,'tmax':high,'tmin':low}]


@pytest.mark.parametrize('high,low,expected', [(29,22,'白天偏热'),(23,18,'白天温和'),(19,13,'白天偏凉'),(12,8,'白天偏冷')])
def test_forecast_changes_context(high, low, expected):
    result = temperature_context(rows(high,low), NOW.isoformat(), NOW)
    assert expected in result
    assert not any(c.isdigit() for c in result)
    assert '雨' not in result


@pytest.mark.parametrize('data,stamp', [(rows(),NOW-timedelta(hours=31)),(rows(),NOW+timedelta(hours=1)),(rows(date='2026-11-06'),NOW),(rows(None,18),NOW),(rows(12,22),NOW)])
def test_unusable_forecast_is_omitted(data, stamp):
    assert temperature_context(data, stamp.isoformat(), NOW) == ''


def test_previous_evening_tomorrow_becomes_today():
    assert '偏热' in temperature_context(rows(), (NOW-timedelta(hours=17)).isoformat(), NOW)


def test_hook_owner_only_without_network(monkeypatch):
    cls = production_class()
    monkeypatch.setitem(cls.inject_local_temperature.__globals__, 'DEFAULT_OWNER_UMO', 'qq_napcat_100000002:FriendMessage:100000001')
    plugin = cls.__new__(cls)
    plugin.config = {'location_label':'广州天河'}
    plugin._state = {'temperature_background':{'city_id':'101010100', 'captured_at': NOW.isoformat(), 'rows':rows()}}
    monkeypatch.setitem(cls.inject_local_temperature.__globals__, 'datetime', SimpleNamespace(now=lambda tz:NOW))
    request = SimpleNamespace(system_prompt='existing')
    owner = SimpleNamespace(unified_msg_origin='qq_napcat_100000002:FriendMessage:100000001')
    asyncio.run(plugin.inject_local_temperature(owner, request))
    assert request.system_prompt.startswith('existing')
    assert '白天偏热' in request.system_prompt and '广州天河' in request.system_prompt
    other = SimpleNamespace(unified_msg_origin='other')
    untouched = SimpleNamespace(system_prompt='existing')
    asyncio.run(plugin.inject_local_temperature(other, untouched))
    assert untouched.system_prompt == 'existing'
