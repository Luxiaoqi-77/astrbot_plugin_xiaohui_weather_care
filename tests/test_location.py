import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from logic import parse_noon_probability, resolve_weather_location as resolve_profile

PROFILES = {
 "广州": {"aliases":["天河"], "location_name":"天河", "location_label":"广州天河", "city_id":"101280109", "probability_location_key":"2332593", "probability_location_name":"天河区"},
 "重庆": {"aliases":["巴南区","重庆市巴南区"], "location_name":"巴南", "location_label":"重庆巴南", "city_id":"101040900", "probability_location_key":"1715563", "probability_location_name":"巴南区"},
}
def resolve_weather_location(name): return resolve_profile(name, PROFILES)
from test_fetch import production_class
from test_noon import page


@pytest.mark.parametrize('name,city', [('重庆','101040900'),('巴南区','101040900'),('重庆市巴南区','101040900'),('广州','101280109')])
def test_profile(name, city):
    assert resolve_weather_location(name)['city_id'] == city


def test_probability_region_validation():
    now=datetime(2026,11,7,12,tzinfo=ZoneInfo('Asia/Shanghai'))
    s=page().replace('101924','1715563').replace(json.dumps('北京'),json.dumps('巴南区'))
    assert parse_noon_probability(s,now,'1715563','巴南区') == 60
    with pytest.raises(ValueError): parse_noon_probability(s,now)


@pytest.mark.parametrize('owner,fail,expected', [(True,False,True),(False,False,False),(True,True,False)])
def test_switch_clears_old_cache_preserves_reminder_dedup(monkeypatch, owner, fail, expected):
    cls=production_class()
    ns=cls.tool_set_weather_location.__globals__
    monkeypatch.setitem(ns,'DEFAULT_OWNER_UMO','owner')
    monkeypatch.setitem(ns,'logger',logging.getLogger('test'))
    class Config(dict):
        def save_config(self):
            if fail: raise OSError('simulated')
    p=cls.__new__(cls)
    p.config=Config(resolve_weather_location('广州')); p.config["location_profiles"]=PROFILES
    p._state={'temperature_background':{'city_id':'101010100'},'noon_check_date':'2026-11-07'}
    p._save_state=lambda: None
    async def fetch(days): raise RuntimeError('no network')
    p._fetch_daily=fetch
    event=SimpleNamespace(unified_msg_origin='owner' if owner else 'other')
    result=asyncio.run(p.tool_set_weather_location(event,'重庆'))
    assert result['ok'] == expected
    assert p._state['noon_check_date'] == '2026-11-07'
    if expected:
        assert p.config['city_id'] == '101040900'
        assert 'temperature_background' not in p._state
    else: assert p.config['city_id'] == '101280109'


def test_location_tool_rejects_unsupported(monkeypatch):
    cls=production_class()
    monkeypatch.setitem(cls.tool_set_weather_location.__globals__,'DEFAULT_OWNER_UMO','owner')
    p=cls.__new__(cls);p.config={}
    assert not asyncio.run(p.tool_set_weather_location(SimpleNamespace(unified_msg_origin='owner'),'未知地区'))['ok']
