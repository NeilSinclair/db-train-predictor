"""Shared HTTP setup for the APIs we call.

Holds the DB API Marketplace config and client factory used by both the
StaDa and Timetables clients, and a request helper that retries temporary
failures. The collector runs for weeks, so one 503 mustn't stop it.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv

# Rate limiting (429) and temporary server problems are worth retrying.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class ApiConfig:
    """Connection settings for one DB API Marketplace API.

    Attributes:
        client_id: DB API Marketplace client ID.
        api_key: DB API Marketplace API key.
        endpoint: Base URL of the API, without a trailing slash.
    """

    client_id: str
    api_key: str
    endpoint: str

    @classmethod
    def from_env(cls, endpoint_var: str) -> ApiConfig:
        """Builds a config from environment variables, loading ``.env`` first.

        Args:
            endpoint_var: Name of the variable holding the API's base URL,
                e.g. ``"DB_TIMETABLES_ENDPOINT"``.

        Returns:
            An ApiConfig populated from ``DB_CLIENT_ID``, ``DB_API_KEY`` and
            ``endpoint_var``.

        Raises:
            KeyError: If any of the required variables is missing.
        """
        load_dotenv()
        names = ("DB_CLIENT_ID", "DB_API_KEY", endpoint_var)
        missing = [name for name in names if not os.environ.get(name)]
        if missing:
            raise KeyError(f"Missing environment variables: {', '.join(missing)}")
        return cls(
            client_id=os.environ["DB_CLIENT_ID"],
            api_key=os.environ["DB_API_KEY"],
            endpoint=os.environ[endpoint_var].rstrip("/"),
        )


def build_client(
    config: ApiConfig, accept: str, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """Creates an HTTP client with the DB auth headers set.

    Args:
        config: Connection settings for the API.
        accept: The response type to ask for, e.g. ``"application/json"``.
        transport: Optional transport override, mainly for tests.

    Returns:
        An httpx.Client pointed at the API's endpoint.
    """
    return httpx.Client(
        base_url=config.endpoint,
        headers={
            "DB-Client-Id": config.client_id,
            "DB-Api-Key": config.api_key,
            "Accept": accept,
        },
        timeout=30.0,
        transport=transport,
    )


def retry_delay(response: httpx.Response | None, attempt: int, backoff_seconds: float) -> float:
    """Works out how long to wait before the next attempt.

    Args:
        response: The failed response, or None if the request didn't get one.
        attempt: How many attempts have failed so far (1 for the first).
        backoff_seconds: The wait after the first failure; it doubles each time.

    Returns:
        The server's ``Retry-After`` value in seconds if it sent a number,
        otherwise the exponential backoff.
    """
    if response is not None:
        retry_after = response.headers.get("Retry-After", "")
        if retry_after.isdigit():
            return float(retry_after)
    return backoff_seconds * 2 ** (attempt - 1)


def request_with_retries(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    attempts: int = 4,
    backoff_seconds: float = 2.0,
    max_delay_seconds: float = 60.0,
    sleep: Callable[[float], None] | None = None,
    **kwargs,
) -> httpx.Response:
    """Sends a request, retrying rate limits, server errors and network errors.

    Other responses, including errors such as 400 or 401, are returned
    straight away for the caller to handle.

    Args:
        client: The HTTP client to send with.
        method: The HTTP method, e.g. ``"GET"``.
        url: The URL or path to request.
        attempts: The maximum number of tries, including the first.
        backoff_seconds: The wait after the first failure; it doubles each time.
        max_delay_seconds: The longest single wait, also when the server's
            ``Retry-After`` asks for more.
        sleep: Called to wait between tries; defaults to ``time.sleep``.
        **kwargs: Passed on to ``httpx.Client.request`` (e.g. ``params``).

    Returns:
        The first response that isn't retryable, or the last response if
        every try was retryable.

    Raises:
        httpx.TransportError: If the last try fails without a response.
    """
    sleep = sleep or time.sleep
    for attempt in range(1, attempts + 1):
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError:
            if attempt == attempts:
                raise
            sleep(min(retry_delay(None, attempt, backoff_seconds), max_delay_seconds))
            continue
        if response.status_code not in RETRY_STATUSES or attempt == attempts:
            return response
        sleep(min(retry_delay(response, attempt, backoff_seconds), max_delay_seconds))
    raise AssertionError("unreachable")  # pragma: no cover
