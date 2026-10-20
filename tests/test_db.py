"""Tests for the SQLite storage module."""

import sqlite3

import pytest

from db_ml.db import (
    collected_evas,
    connect,
    database_path,
    init_db,
    sync_station_evas,
    upsert_stations,
)

BERLIN = {
    "stationNumber": 1071,
    "name": "Berlin Hauptbahnhof",
    "city": "Berlin",
    "municipalityCode": "11000000",
    "population": 3_700_577,
    "latitude": 52.525592,
    "longitude": 13.369545,
}


def berlin_eva(eva: int, collect: bool, name: str | None = "Berlin Hbf") -> dict:
    """Builds a station_evas record for Berlin Hbf.

    Args:
        eva: The EVA number.
        collect: Whether to collect it.
        name: The Timetables station name.

    Returns:
        A dict shaped like a station_evas.json record.
    """
    return {"eva": eva, "stationNumber": 1071, "timetableName": name, "collect": collect}


@pytest.fixture
def conn():
    """Provides an in-memory database with the schema created.

    Yields:
        An open SQLite connection.
    """
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()


def test_connect_creates_folder_and_uses_wal(tmp_path):
    path = tmp_path / "nested" / "db.sqlite"

    connection = connect(path)

    assert path.exists()
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    connection.close()


def test_database_path_defaults_and_can_be_overridden(monkeypatch):
    monkeypatch.delenv("DB_ML_DATABASE_PATH", raising=False)
    assert str(database_path()) == "data/db_ml.sqlite"

    monkeypatch.setenv("DB_ML_DATABASE_PATH", "/var/lib/db-ml/db.sqlite")
    assert str(database_path()) == "/var/lib/db-ml/db.sqlite"


def test_init_db_creates_collector_tables_and_is_repeatable(conn):
    init_db(conn)

    tables = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert tables == {"stations", "station_evas", "stops", "stop_changes", "stop_messages"}


def test_upsert_stations_inserts_then_updates(conn):
    upsert_stations(conn, [BERLIN])
    upsert_stations(conn, [{**BERLIN, "population": 3_800_000}])

    rows = conn.execute("SELECT * FROM stations").fetchall()
    assert len(rows) == 1
    assert rows[0]["population"] == 3_800_000
    assert rows[0]["municipality_code"] == "11000000"


def test_sync_station_evas_and_collected_evas(conn):
    upsert_stations(conn, [BERLIN])
    sync_station_evas(conn, [
        berlin_eva(8011160, False, None),
        berlin_eva(8098160, True),
        berlin_eva(8089021, False, "Berlin Hbf (S-Bahn)"),
    ])

    assert collected_evas(conn) == [8098160]

    # Switching S-Bahn on later is just an update.
    sync_station_evas(conn, [
        berlin_eva(8011160, False, None),
        berlin_eva(8098160, True),
        berlin_eva(8089021, True, "Berlin Hbf (S-Bahn)"),
    ])
    assert collected_evas(conn) == [8089021, 8098160]


def test_sync_station_evas_switches_off_evas_no_longer_listed(conn):
    upsert_stations(conn, [BERLIN])
    sync_station_evas(conn, [berlin_eva(8098160, True), berlin_eva(8089021, True)])

    sync_station_evas(conn, [berlin_eva(8098160, True)])

    assert collected_evas(conn) == [8098160]
    # Kept, not deleted, because collected stops may refer to it.
    assert conn.execute("SELECT collect FROM station_evas WHERE eva = 8089021").fetchone()[0] == 0


def test_sync_station_evas_with_empty_list_switches_everything_off(conn):
    upsert_stations(conn, [BERLIN])
    sync_station_evas(conn, [berlin_eva(8098160, True)])

    sync_station_evas(conn, [])

    assert collected_evas(conn) == []


def test_station_evas_must_belong_to_a_known_station(conn):
    with pytest.raises(sqlite3.IntegrityError):
        sync_station_evas(conn, [berlin_eva(8098160, True)])


def test_stops_reject_unknown_eva(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO stops (stop_id, trip_id, trip_start, stop_index, eva,
                               first_seen_at, last_updated_at)
            VALUES ('x-2610200804-1', 'x', '2026-10-20T08:04:00+02:00', 1, 999,
                    '2026-10-20T18:00:00+02:00', '2026-10-20T18:00:00+02:00')
            """
        )
