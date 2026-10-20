"""Tests for the shared API config, client and retry helper."""

import httpx
import pytest

from db_ml import api
from db_ml.api import ApiConfig, build_client, request_with_retries, retry_delay

CONFIG = ApiConfig("test-client-id", "test-api-key", "https://example.test/v1")


def client_returning(*responses):
    """Builds a client that returns the given responses in order.

    Args:
        *responses: httpx.Response objects, or exceptions to raise instead.

    Returns:
        A tuple of the client and the list of requests it received.
    """
    queue = list(responses)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return build_client(CONFIG, "application/json", httpx.MockTransport(handler)), seen


def test_config_from_env(monkeypatch):
    monkeypatch.setattr(api, "load_dotenv", lambda: None)
    monkeypatch.setenv("DB_CLIENT_ID", "id")
    monkeypatch.setenv("DB_API_KEY", "key")
    monkeypatch.setenv("SOME_ENDPOINT", "https://example.test/v2/")

    assert ApiConfig.from_env("SOME_ENDPOINT") == ApiConfig("id", "key", "https://example.test/v2")


def test_config_from_env_reports_missing_variables(monkeypatch):
    monkeypatch.setattr(api, "load_dotenv", lambda: None)
    monkeypatch.setenv("DB_CLIENT_ID", "id")
    monkeypatch.delenv("DB_API_KEY", raising=False)
    monkeypatch.delenv("SOME_ENDPOINT", raising=False)

    with pytest.raises(KeyError, match="DB_API_KEY, SOME_ENDPOINT"):
        ApiConfig.from_env("SOME_ENDPOINT")


def test_build_client_sets_auth_headers_base_url_and_accept():
    client = build_client(CONFIG, "application/xml")

    assert client.headers["DB-Client-Id"] == "test-client-id"
    assert client.headers["DB-Api-Key"] == "test-api-key"
    assert client.headers["Accept"] == "application/xml"
    assert str(client.base_url) == "https://example.test/v1/"


def test_retries_server_errors_with_doubling_backoff():
    client, seen = client_returning(
        httpx.Response(503), httpx.Response(502), httpx.Response(200, text="ok")
    )
    sleeps: list[float] = []

    response = request_with_retries(client, "GET", "/x", backoff_seconds=2, sleep=sleeps.append)

    assert response.text == "ok"
    assert len(seen) == 3
    assert sleeps == [2, 4]


def test_retry_follows_retry_after_header():
    client, _ = client_returning(
        httpx.Response(429, headers={"Retry-After": "30"}), httpx.Response(200)
    )
    sleeps: list[float] = []

    request_with_retries(client, "GET", "/x", sleep=sleeps.append)

    assert sleeps == [30]


def test_does_not_retry_client_errors():
    client, seen = client_returning(httpx.Response(401))

    response = request_with_retries(client, "GET", "/x", sleep=lambda s: None)

    assert response.status_code == 401
    assert len(seen) == 1


def test_returns_last_response_when_retries_run_out():
    client, seen = client_returning(*[httpx.Response(503) for _ in range(3)])

    response = request_with_retries(client, "GET", "/x", attempts=3, sleep=lambda s: None)

    assert response.status_code == 503
    assert len(seen) == 3


def test_retries_network_errors_then_raises():
    error = httpx.ConnectError("connection refused")
    client, seen = client_returning(error, error)

    with pytest.raises(httpx.ConnectError):
        request_with_retries(client, "GET", "/x", attempts=2, sleep=lambda s: None)
    assert len(seen) == 2


def test_recovers_after_a_network_error():
    client, _ = client_returning(httpx.ReadTimeout("slow"), httpx.Response(200))

    response = request_with_retries(client, "GET", "/x", sleep=lambda s: None)

    assert response.status_code == 200


def test_passes_request_options_through():
    client, seen = client_returning(httpx.Response(200))

    request_with_retries(client, "GET", "/stations", params={"limit": 5}, sleep=lambda s: None)

    assert seen[0].url.params["limit"] == "5"


@pytest.mark.parametrize(
    ("headers", "attempt", "expected"),
    [({}, 1, 2.0), ({}, 3, 8.0), ({"Retry-After": "7"}, 1, 7.0), ({"Retry-After": "soon"}, 2, 4.0)],
)
def test_retry_delay(headers, attempt, expected):
    assert retry_delay(httpx.Response(429, headers=headers), attempt, 2.0) == expected
