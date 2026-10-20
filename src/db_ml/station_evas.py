"""Decides which EVA numbers to collect for each station.

A station can have several EVA numbers, and StaDa's ``isMain`` one doesn't
always have trains in the Timetables API (Berlin's doesn't). This module
asks the Timetables API what trains each EVA number has during two sample
hours and marks an EVA for collection unless it's (almost) entirely S-Bahn
(decisions D6 and D8).

Example:
    Write ``data/processed/station_evas.json``::

        $ uv run python -m db_ml.station_evas
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from db_ml.files import load_json, save_json
from db_ml.timetable_xml import S_BAHN_CATEGORY
from db_ml.timetables import (
    MIN_SECONDS_BETWEEN_REQUESTS,
    build_client,
    fetch_plan,
    load_config,
    train_categories,
)

STATIONS_PATH = Path("data/raw/stations_clean.json")
TOP_STATIONS_PATH = Path("data/processed/top_stations.json")
DEFAULT_OUTPUT_PATH = Path("data/processed/station_evas.json")
# EVAs where at least this share of trains is S-Bahn count as S-Bahn only.
S_BAHN_ONLY_SHARE = 0.9
# A morning and an evening peak, so one quiet hour can't drop an EVA.
SAMPLE_HOURS = (8, 17)


def should_collect(categories: list[str]) -> bool:
    """Decides whether an EVA number is worth collecting.

    Many stations (e.g. Bonn, Essen, Dresden) list S-Bahn and main-line
    trains under the same EVA number, so a high S-Bahn share alone doesn't
    disqualify an EVA. The collector skips individual S-Bahn trains anyway.
    Only EVAs that are (almost) entirely S-Bahn, such as underground
    platforms, are left out.

    Args:
        categories: The category of each train seen at the EVA number.

    Returns:
        True if the EVA has trains and less than 90% of them are S-Bahn.
    """
    if not categories:
        return False
    s_bahn = sum(1 for category in categories if category == S_BAHN_CATEGORY)
    return s_bahn / len(categories) < S_BAHN_ONLY_SHARE


def probe_station_evas(
    client: httpx.Client,
    stations: list[dict[str, Any]],
    hours: list[datetime],
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Checks every EVA number of the given stations against the Timetables API.

    Args:
        client: A Timetables HTTP client.
        stations: StaDa station records, each with ``number`` and
            ``evaNumbers``.
        hours: The hours to sample, in German local time. Trains from all of
            them are counted together.
        sleep: Called between requests to stay under the rate limit;
            replaceable in tests.

    Returns:
        One record per EVA number with its station number, the name the
        Timetables API uses, train counts and whether to collect it.
    """
    results = []
    for station in stations:
        for eva in station.get("evaNumbers", []):
            categories: list[str] = []
            timetable_name = None
            for hour in hours:
                timetable = fetch_plan(client, eva["number"], hour)
                sleep(MIN_SECONDS_BETWEEN_REQUESTS)
                if timetable is not None:
                    categories += train_categories(timetable)
                    timetable_name = timetable_name or timetable.get("station")
            results.append(
                {
                    "eva": eva["number"],
                    "stationNumber": station["number"],
                    "stationName": station["name"],
                    "timetableName": timetable_name,
                    "isStadaMain": bool(eva.get("isMain")),
                    "trains": len(categories),
                    "sBahnTrains": categories.count(S_BAHN_CATEGORY),
                    "collect": should_collect(categories),
                }
            )
    return results


def stations_without_collected_eva(evas: list[dict[str, Any]]) -> list[str]:
    """Finds stations where no EVA number was marked for collection.

    Args:
        evas: Records returned by ``probe_station_evas``.

    Returns:
        Names of stations with nothing to collect, which need a manual look.
    """
    collected = {e["stationNumber"] for e in evas if e["collect"]}
    return sorted({e["stationName"] for e in evas if e["stationNumber"] not in collected})


def main() -> None:
    """Probes the top stations' EVA numbers and saves the result."""
    top_numbers = {s["stationNumber"] for s in load_json(TOP_STATIONS_PATH)}
    stations = [s for s in load_json(STATIONS_PATH) if s["number"] in top_numbers]
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    hours = [now.replace(hour=hour) for hour in SAMPLE_HOURS]

    with build_client(load_config()) as client:
        evas = probe_station_evas(client, stations, hours)

    save_json(evas, DEFAULT_OUTPUT_PATH)
    to_collect = sum(e["collect"] for e in evas)
    print(f"Saved {len(evas)} EVA numbers ({to_collect} to collect) to {DEFAULT_OUTPUT_PATH}")
    if missing := stations_without_collected_eva(evas):
        print(f"No EVA to collect for: {', '.join(missing)}")


if __name__ == "__main__":
    main()
