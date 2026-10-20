"""Client for the Deutsche Bahn Timetables API.

The API is per station: it takes an EVA number and returns XML listing the
trains stopping there. Credentials and the endpoint are read from the
environment (see ``.env``).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime

import httpx

from db_ml.api import ApiConfig, request_with_retries
from db_ml.api import build_client as build_api_client

ENDPOINT_VAR = "DB_TIMETABLES_ENDPOINT"
# The Timetables API allows 60 requests per minute.
MIN_SECONDS_BETWEEN_REQUESTS = 1.05


def load_config() -> ApiConfig:
    """Reads the Timetables connection settings from the environment.

    Returns:
        An ApiConfig using ``DB_TIMETABLES_ENDPOINT`` as the base URL.

    Raises:
        KeyError: If any of the required variables is missing.
    """
    return ApiConfig.from_env(ENDPOINT_VAR)


def build_client(
    config: ApiConfig, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """Creates an HTTP client for the Timetables API.

    Args:
        config: Connection settings for the Timetables API.
        transport: Optional transport override, mainly for tests.

    Returns:
        An httpx.Client that asks for XML responses.
    """
    return build_api_client(config, "application/xml", transport)


def fetch_plan(client: httpx.Client, eva: int, hour: datetime) -> ET.Element | None:
    """Fetches the planned timetable for one station and one hour.

    Args:
        client: An HTTP client created by ``build_client``.
        eva: The station's EVA number.
        hour: Any time within the wanted hour, in German local time.

    Returns:
        The parsed ``<timetable>`` element, or None if the API rejects the
        EVA number (HTTP 400 or 404).

    Raises:
        httpx.HTTPStatusError: For any other error status, once retries
            are used up.
    """
    response = request_with_retries(client, "GET", f"/plan/{eva}/{hour:%y%m%d}/{hour:%H}")
    if response.status_code in (400, 404):
        return None
    response.raise_for_status()
    return ET.fromstring(response.content)


def train_categories(timetable: ET.Element) -> list[str]:
    """Lists the category of every train in a timetable response.

    Args:
        timetable: A parsed ``<timetable>`` element.

    Returns:
        One category per train (e.g. ``"ICE"``, ``"RE"``, ``"S"``), in the
        order they appear. Trains without a category are left out.
    """
    categories = []
    for stop in timetable.findall("s"):
        train = stop.find("tl")
        if train is not None and train.get("c"):
            categories.append(train.get("c"))
    return categories
