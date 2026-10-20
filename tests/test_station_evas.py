"""Tests for choosing which EVA numbers to collect."""

from datetime import datetime

import httpx
import pytest

from db_ml.station_evas import (
    probe_station_evas,
    should_collect,
    stations_without_collected_eva,
)
from db_ml.api import ApiConfig
from db_ml.timetables import build_client

CONFIG = ApiConfig("id", "key", "https://example.test/timetables/v1")


def timetable_xml(name: str, categories: list[str]) -> str:
    """Builds a fake /plan response.

    Args:
        name: The station name the API reports.
        categories: One train category per train.

    Returns:
        XML text shaped like a Timetables /plan response.
    """
    stops = "".join(
        f'<s id="{i}-2610200800-1"><tl c="{c}" n="{i}"/></s>' for i, c in enumerate(categories)
    )
    return f"<timetable station='{name}'>{stops}</timetable>"


@pytest.mark.parametrize(
    ("categories", "expected"),
    [
        ([], False),                          # no trains (Berlin's StaDa main EVA)
        (["S"] * 42, False),                  # S-Bahn only
        (["S"] * 19 + ["MDS"], False),        # Leipzig (tief): 95% S-Bahn
        (["S"] * 9 + ["RE"], False),          # exactly 90% counts as S-Bahn only
        (["S"] * 16 + ["RE"] * 3, True),      # Ludwigshafen: 84% S, shared EVA
        (["S"] * 8 + ["ICE"] * 8, True),      # Bonn: half S-Bahn, shared EVA
        (["S"] * 6 + ["RE"] * 23, True),      # München Gl.27-36: mostly regional
        (["ICE", "RE"], True),
    ],
)
def test_should_collect(categories, expected):
    assert should_collect(categories) is expected


HOURS = [datetime(2026, 10, 20, 8), datetime(2026, 10, 20, 17)]


def test_probe_station_evas_classifies_each_eva_and_waits_between_requests():
    # Keyed by (EVA, hour). Berlin Hbf's trains are split across both hours.
    responses = {
        ("8011160", "08"): "<timetable/>",
        ("8011160", "17"): "<timetable/>",
        ("8089021", "08"): timetable_xml("Berlin Hbf (S-Bahn)", ["S"]),
        ("8089021", "17"): timetable_xml("Berlin Hbf (S-Bahn)", ["S"]),
        ("8098160", "08"): timetable_xml("Berlin Hbf", ["ICE", "RE"]),
        ("8098160", "17"): timetable_xml("Berlin Hbf", ["S"]),
    }
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        eva, date, hour = request.url.path.split("/")[-3:]
        assert date == "261020"
        if eva == "8089318":
            return httpx.Response(400)
        return httpx.Response(200, text=responses[(eva, hour)])

    stations = [
        {"number": 1071, "name": "Berlin Hauptbahnhof", "evaNumbers": [
            {"number": 8011160, "isMain": True},
            {"number": 8089021, "isMain": False},
            {"number": 8098160, "isMain": False},
        ]},
        {"number": 3631, "name": "Leipzig Hbf", "evaNumbers": [{"number": 8089318}]},
    ]
    sleeps: list[float] = []
    client = build_client(CONFIG, httpx.MockTransport(handler))

    result = probe_station_evas(client, stations, HOURS, sleeps.append)

    assert [(e["eva"], e["collect"]) for e in result] == [
        (8011160, False), (8089021, False), (8098160, True), (8089318, False),
    ]
    assert result[2] == {
        "eva": 8098160,
        "stationNumber": 1071,
        "stationName": "Berlin Hauptbahnhof",
        "timetableName": "Berlin Hbf",
        "isStadaMain": False,
        "trains": 3,
        "sBahnTrains": 1,
        "collect": True,
    }
    assert result[3]["timetableName"] is None
    # 4 EVAs x 2 hours, each followed by a rate-limit pause.
    assert len(seen_paths) == 8
    assert len(sleeps) == 8 and all(s >= 1 for s in sleeps)


def test_probe_counts_trains_from_every_sampled_hour():
    # Quiet in the morning, busy in the evening: must still be collected.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/08"):
            return httpx.Response(200, text="<timetable station='Kassel Hbf'/>")
        return httpx.Response(200, text=timetable_xml("Kassel Hbf", ["ICE", "RE"]))

    stations = [{"number": 3127, "name": "Kassel Hbf", "evaNumbers": [{"number": 8000193}]}]
    client = build_client(CONFIG, httpx.MockTransport(handler))

    [result] = probe_station_evas(client, stations, HOURS, lambda s: None)

    assert result["trains"] == 2
    assert result["collect"] is True
    assert result["timetableName"] == "Kassel Hbf"


def test_stations_without_collected_eva():
    evas = [
        {"stationNumber": 1, "stationName": "A Hbf", "collect": True},
        {"stationNumber": 1, "stationName": "A Hbf", "collect": False},
        {"stationNumber": 2, "stationName": "B Hbf", "collect": False},
    ]

    assert stations_without_collected_eva(evas) == ["B Hbf"]
