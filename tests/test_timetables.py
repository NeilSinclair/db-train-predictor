"""Tests for the Timetables API client."""

import xml.etree.ElementTree as ET
from datetime import datetime

import httpx
import pytest

from db_ml import api
from db_ml.api import ApiConfig
from db_ml.timetables import build_client, fetch_plan, load_config, train_categories

CONFIG = ApiConfig(
    client_id="test-client-id",
    api_key="test-api-key",
    endpoint="https://example.test/timetables/v1",
)

PLAN_XML = """<?xml version='1.0' encoding='UTF-8'?>
<timetable station='Berlin Hbf'>
  <s id="-2041012609203177085-2610200804-18">
    <tl f="F" t="p" o="85" c="ICE" n="372"/>
    <ar pt="2610201717" pp="7"/>
  </s>
  <s id="1-2610201700-3"><tl c="RE" n="3"/><dp pt="2610201712" pp="2"/></s>
  <s id="2-2610201700-4"><dp pt="2610201715" pp="1"/></s>
</timetable>"""


def test_load_config_uses_timetables_endpoint(monkeypatch):
    monkeypatch.setattr(api, "load_dotenv", lambda: None)
    monkeypatch.setenv("DB_CLIENT_ID", "id")
    monkeypatch.setenv("DB_API_KEY", "key")
    monkeypatch.setenv("DB_TIMETABLES_ENDPOINT", "https://example.test/timetables/v1")

    assert load_config().endpoint == "https://example.test/timetables/v1"


def test_build_client_asks_for_xml():
    client = build_client(CONFIG)

    assert client.headers["Accept"] == "application/xml"
    assert client.headers["DB-Client-Id"] == "test-client-id"


def test_fetch_plan_requests_date_and_hour_and_parses_xml():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=PLAN_XML)

    client = build_client(CONFIG, httpx.MockTransport(handler))

    timetable = fetch_plan(client, 8098160, datetime(2026, 10, 20, 17, 30))

    assert seen[0].url.path == "/timetables/v1/plan/8098160/261020/17"
    assert timetable.get("station") == "Berlin Hbf"


@pytest.mark.parametrize("status", [400, 404])
def test_fetch_plan_returns_none_for_unknown_eva(status):
    client = build_client(CONFIG, httpx.MockTransport(lambda r: httpx.Response(status)))

    assert fetch_plan(client, 8089318, datetime(2026, 10, 20, 8)) is None


def test_fetch_plan_raises_on_other_errors():
    client = build_client(CONFIG, httpx.MockTransport(lambda r: httpx.Response(401)))

    with pytest.raises(httpx.HTTPStatusError):
        fetch_plan(client, 8098160, datetime(2026, 10, 20, 8))


def test_fetch_plan_retries_a_temporary_failure(monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda seconds: None)
    responses = [httpx.Response(503), httpx.Response(200, text=PLAN_XML)]
    client = build_client(CONFIG, httpx.MockTransport(lambda r: responses.pop(0)))

    timetable = fetch_plan(client, 8098160, datetime(2026, 10, 20, 17))

    assert timetable.get("station") == "Berlin Hbf"


def test_train_categories_skips_stops_without_category():
    assert train_categories(ET.fromstring(PLAN_XML)) == ["ICE", "RE"]
