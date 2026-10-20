"""Client for the Deutsche Bahn Station Data (StaDa) API.

Pulls the full list of stations from the DB API Marketplace and can save
them to a JSON file. Credentials and the endpoint are read from the
environment (see ``.env``).

Example:
    Fetch all stations and write them to ``data/raw/stations.json``::

        $ uv run python -m db_ml.stations
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from db_ml.api import ApiConfig, request_with_retries
from db_ml.api import build_client as build_api_client
from db_ml.files import save_json

ENDPOINT_VAR = "DB_STATIONS_ENDPOINT"
# The StaDa API returns at most 10,000 stations per request.
MAX_PAGE_SIZE = 10_000
DEFAULT_OUTPUT_PATH = Path("data/raw/stations.json")


def load_config() -> ApiConfig:
    """Reads the StaDa connection settings from the environment.

    Returns:
        An ApiConfig using ``DB_STATIONS_ENDPOINT`` as the base URL.

    Raises:
        KeyError: If any of the required variables is missing.
    """
    return ApiConfig.from_env(ENDPOINT_VAR)


def build_client(
    config: ApiConfig, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """Creates an HTTP client for the StaDa API.

    Args:
        config: Connection settings for the StaDa API.
        transport: Optional transport override, mainly for tests.

    Returns:
        An httpx.Client that asks for JSON responses.
    """
    return build_api_client(config, "application/json", transport)


def fetch_stations(
    client: httpx.Client, page_size: int = MAX_PAGE_SIZE
) -> list[dict[str, Any]]:
    """Fetches every station from the StaDa API, following pagination.

    Args:
        client: An HTTP client created by ``build_client``.
        page_size: Number of stations to request per page (1 to 10,000).

    Returns:
        A list of station records exactly as returned by the API.

    Raises:
        ValueError: If ``page_size`` is outside the allowed range, or a
            response doesn't say how many stations there are in total.
        httpx.HTTPStatusError: If the API returns an error status.
    """
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")

    stations: list[dict[str, Any]] = []
    offset = 0
    while True:
        response = request_with_retries(
            client, "GET", "/stations", params={"offset": offset, "limit": page_size}
        )
        response.raise_for_status()
        body = response.json()
        if "total" not in body:
            # Without it we can't tell whether more pages follow.
            raise ValueError("StaDa response has no 'total' field")
        page = body.get("result", [])
        stations.extend(page)
        offset += len(page)
        if not page or offset >= body["total"]:
            return stations


def main() -> None:
    """Fetches all stations and saves them to ``data/raw/stations.json``."""
    with build_client(load_config()) as client:
        stations = fetch_stations(client)
    save_json(stations, DEFAULT_OUTPUT_PATH)
    print(f"Saved {len(stations)} stations to {DEFAULT_OUTPUT_PATH}")


if __name__ == "__main__":
    main()
