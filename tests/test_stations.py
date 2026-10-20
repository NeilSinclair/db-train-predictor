"""Tests for the StaDa stations client."""

import httpx
import pytest

from db_ml import api
from db_ml.api import ApiConfig
from db_ml.stations import build_client, fetch_stations, load_config

CONFIG = ApiConfig(
    client_id="test-client-id",
    api_key="test-api-key",
    endpoint="https://example.test/station-data/v2",
)


def make_station(number: int) -> dict:
    """Builds a minimal fake station record.

    Args:
        number: The station number to use.

    Returns:
        A dict shaped like a StaDa station record.
    """
    return {"number": number, "name": f"Station {number}"}


def paged_handler(all_stations: list[dict], seen: list[httpx.Request]):
    """Creates a mock handler that serves stations in pages like StaDa does.

    Args:
        all_stations: The full set of stations the fake API holds.
        seen: A list that every incoming request is appended to.

    Returns:
        A handler function for httpx.MockTransport.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        offset = int(request.url.params["offset"])
        limit = int(request.url.params["limit"])
        return httpx.Response(
            200,
            json={
                "offset": offset,
                "limit": limit,
                "total": len(all_stations),
                "result": all_stations[offset : offset + limit],
            },
        )

    return handler


def test_load_config_uses_stations_endpoint(monkeypatch):
    monkeypatch.setattr(api, "load_dotenv", lambda: None)
    monkeypatch.setenv("DB_CLIENT_ID", "id")
    monkeypatch.setenv("DB_API_KEY", "key")
    monkeypatch.setenv("DB_STATIONS_ENDPOINT", "https://example.test/station-data/v2")

    assert load_config().endpoint == "https://example.test/station-data/v2"


def test_build_client_asks_for_json():
    client = build_client(CONFIG)

    assert client.headers["Accept"] == "application/json"
    assert client.headers["DB-Api-Key"] == "test-api-key"


def test_fetch_stations_single_page():
    seen: list[httpx.Request] = []
    data = [make_station(i) for i in range(3)]
    client = build_client(CONFIG, httpx.MockTransport(paged_handler(data, seen)))

    result = fetch_stations(client)

    assert result == data
    assert len(seen) == 1
    assert seen[0].url.path == "/station-data/v2/stations"


def test_fetch_stations_follows_pagination():
    seen: list[httpx.Request] = []
    data = [make_station(i) for i in range(5)]
    client = build_client(CONFIG, httpx.MockTransport(paged_handler(data, seen)))

    result = fetch_stations(client, page_size=2)

    assert result == data
    assert [r.url.params["offset"] for r in seen] == ["0", "2", "4"]


def test_fetch_stations_stops_on_empty_page():
    # Guards against looping forever if the API reports a wrong total.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total": 100, "result": []})

    client = build_client(CONFIG, httpx.MockTransport(handler))

    assert fetch_stations(client) == []


def test_fetch_stations_rejects_response_without_total():
    # Without "total" we can't tell if more pages follow, so don't guess.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": [make_station(1)]})

    client = build_client(CONFIG, httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="total"):
        fetch_stations(client)


def test_fetch_stations_raises_on_http_error():
    client = build_client(
        CONFIG, httpx.MockTransport(lambda request: httpx.Response(401))
    )

    with pytest.raises(httpx.HTTPStatusError):
        fetch_stations(client)


@pytest.mark.parametrize("page_size", [0, 10_001])
def test_fetch_stations_rejects_invalid_page_size(page_size):
    client = build_client(CONFIG)

    with pytest.raises(ValueError):
        fetch_stations(client, page_size=page_size)
