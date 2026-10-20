"""Tests for the SQLite storage module."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from db_ml.db import (
    MIGRATIONS,
    collected_evas,
    connect,
    database_path,
    fetched_plan_hours,
    init_db,
    record_plan_fetch,
    schema_version,
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


def test_new_database_starts_on_the_latest_schema_version(conn):
    assert schema_version(conn) == len(MIGRATIONS) == 2


def test_database_from_before_migrations_is_upgraded_without_losing_data():
    connection = connect(":memory:")
    connection.executescript(MIGRATIONS[0])
    upsert_stations(connection, [BERLIN])
    assert schema_version(connection) == 0

    init_db(connection)

    assert schema_version(connection) == len(MIGRATIONS)
    assert connection.execute("SELECT COUNT(*) FROM stations").fetchone()[0] == 1


def test_init_db_applies_only_pending_migrations(conn):
    upsert_stations(conn, [BERLIN])
    migrations = [*MIGRATIONS, "ALTER TABLE stations ADD COLUMN state TEXT;"]

    init_db(conn, migrations)
    init_db(conn, migrations)

    assert schema_version(conn) == len(migrations)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(stations)")}
    assert "state" in columns
    assert conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0] == 1


def test_failed_migration_is_rolled_back(conn):
    broken = "ALTER TABLE stations ADD COLUMN state TEXT; ALTER TABLE no_such_table ADD COLUMN x;"

    with pytest.raises(sqlite3.OperationalError):
        init_db(conn, [*MIGRATIONS, broken])

    assert schema_version(conn) == len(MIGRATIONS)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(stations)")}
    assert "state" not in columns


def test_init_db_refuses_a_database_newer_than_the_code(conn):
    conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")

    with pytest.raises(RuntimeError, match=f"schema version {len(MIGRATIONS) + 1}"):
        init_db(conn)


def test_init_db_creates_collector_tables_and_is_repeatable(conn):
    init_db(conn)

    tables = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert tables == {
        "stations", "station_evas", "stops", "stop_changes", "stop_messages", "plan_fetches",
    }


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


def test_record_plan_fetch_and_look_it_up(conn):
    upsert_stations(conn, [BERLIN])
    sync_station_evas(conn, [berlin_eva(8098160, True)])
    first = datetime(2026, 10, 20, 18, 0, tzinfo=UTC)

    record_plan_fetch(conn, 8098160, "26102020", first)
    record_plan_fetch(conn, 8098160, "26102020", first + timedelta(minutes=5))
    record_plan_fetch(conn, 8098160, "26102021", first)

    assert fetched_plan_hours(conn, {"26102020", "26102022"}) == {(8098160, "26102020")}
    row = conn.execute("SELECT fetched_at FROM plan_fetches WHERE hour_label = '26102020'").fetchone()
    assert row["fetched_at"] == "2026-10-20T18:05:00+00:00"
