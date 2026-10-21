"""Tests for building station sentences from the collector database."""

import json
from collections import Counter

import pytest

from db_ml.db import connect, init_db, sync_station_evas, upsert_stations
from station2vec import render_html, station_kinds, train_kind, trip_routes


@pytest.fixture
def conn():
    """Provides an in-memory database with Köln and Frankfurt collected.

    Yields:
        An open SQLite connection.
    """
    connection = connect(":memory:")
    init_db(connection)
    blank = {"city": "", "municipalityCode": "", "population": None, "latitude": 0.0, "longitude": 0.0}
    upsert_stations(connection, [
        {**blank, "stationNumber": 1, "name": "Köln Hbf"},
        {**blank, "stationNumber": 2, "name": "Frankfurt (Main) Hbf"},
    ])
    sync_station_evas(connection, [
        {"eva": 8000207, "stationNumber": 1, "timetableName": "Köln Hbf", "collect": True},
        {"eva": 8000105, "stationNumber": 2, "timetableName": "Frankfurt(Main)Hbf", "collect": True},
    ])
    yield connection
    connection.close()


def add_stop(conn, trip_id: str, eva: int, category: str, before: str | None, after: str | None) -> None:
    """Inserts a stop with just the columns the routes need.

    Args:
        conn: The database connection.
        trip_id: The trip the stop belongs to.
        eva: The station's EVA number.
        category: The train category.
        before: Pipe-separated stations before this one, if any.
        after: Pipe-separated stations after this one, if any.
    """
    conn.execute(
        """
        INSERT INTO stops (stop_id, trip_id, trip_start, stop_index, eva, category,
                           route_before, route_after, first_seen_at, last_updated_at)
        VALUES (?, ?, '2610210800', ?, ?, ?, ?, ?, 'now', 'now')
        """,
        (f"{trip_id}-{eva}", trip_id, eva % 100, eva, category, before, after),
    )


def test_route_is_the_stations_before_plus_this_one_plus_after(conn):
    add_stop(conn, "a", 8000207, "RE", "Aachen Hbf|Düren", "Bonn Hbf")

    assert trip_routes(conn) == [(["Aachen Hbf", "Düren", "Köln Hbf", "Bonn Hbf"], "regional")]


def test_first_and_last_stops_have_no_empty_station(conn):
    add_stop(conn, "a", 8000207, "ICE", None, "Siegburg/Bonn")
    add_stop(conn, "b", 8000105, "ICE", "Siegburg/Bonn", None)

    assert sorted(trip_routes(conn)) == [
        (["Köln Hbf", "Siegburg/Bonn"], "long-distance"),
        (["Siegburg/Bonn", "Frankfurt(Main)Hbf"], "long-distance"),
    ]


def test_trip_seen_at_two_stations_counts_once_with_its_longest_route(conn):
    add_stop(conn, "a", 8000207, "ICE", "Dortmund Hbf", "Siegburg/Bonn|Frankfurt(Main)Hbf")
    add_stop(conn, "a", 8000105, "ICE", "Köln Hbf|Siegburg/Bonn", None)

    assert trip_routes(conn) == [
        (["Dortmund Hbf", "Köln Hbf", "Siegburg/Bonn", "Frankfurt(Main)Hbf"], "long-distance"),
    ]


def test_train_kind():
    assert train_kind("ICE") == "long-distance"
    assert train_kind("Bus") == "bus"
    assert train_kind("RB") == "regional"
    assert train_kind(None) == "regional"


def test_render_html_embeds_compact_data_once():
    records = [
        {"name": "A", "tsne": [1.0, 2.0], "pca": [0.1, 0.2], "kinds": {"regional": 3}, "near": [["</script>", 0.9]]},
        {"name": "</script>", "tsne": [0.0, 0.0], "pca": [0.3, 0.4], "kinds": {"bus": 1}, "near": [["A", 0.9]]},
    ]

    page = render_html(records, {"trips": 4}, "<script>const DATA = /*DATA*/null;</script>")

    assert page.count("</script>") == 1
    data = json.loads(page.removeprefix("<script>const DATA = ").removesuffix(";</script>").replace("<\\/", "</"))
    assert data["kinds"] == [[3, 0, 0], [0, 0, 1]]
    assert data["near"] == [[[1, 0.9]], [[0, 0.9]]]
    assert data["proj"] == {"tsne": {"x": [1.0, 0.0], "y": [2.0, 0.0]}, "pca": {"x": [0.1, 0.3], "y": [0.2, 0.4]}}
    assert data["meta"] == {"trips": 4}


def test_station_kinds_counts_each_trip_once_per_station():
    routes = [(["A", "B", "A"], "regional"), (["A", "C"], "long-distance")]

    kinds = station_kinds(routes)

    assert kinds["A"] == Counter({"regional": 1, "long-distance": 1})
    assert kinds["C"] == Counter({"long-distance": 1})
