"""SQLite storage for stations and collected timetable data.

All SQL lives in this module (decision D11), so a later move to Postgres
stays contained. The schema follows decision D7. The ``journeys`` table is
not created here because it's derived from ``stops`` by a later job.

Times are stored as ISO 8601 text with their UTC offset, e.g.
``2026-10-20T17:18:00+02:00``. Booleans are stored as 0 or 1.

Example:
    Create the database and load the reference data::

        $ uv run python -m db_ml.db
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from db_ml.files import load_json

DEFAULT_DATABASE_PATH = Path("data/db_ml.sqlite")
TOP_STATIONS_PATH = Path("data/processed/top_stations.json")
STATION_EVAS_PATH = Path("data/processed/station_evas.json")

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    station_number    INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    city              TEXT NOT NULL,
    municipality_code TEXT NOT NULL,
    population        INTEGER,
    latitude          REAL NOT NULL,
    longitude         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS station_evas (
    eva            INTEGER PRIMARY KEY,
    station_number INTEGER NOT NULL REFERENCES stations (station_number),
    timetable_name TEXT,
    collect        INTEGER NOT NULL CHECK (collect IN (0, 1))
);

CREATE TABLE IF NOT EXISTS stops (
    stop_id             TEXT PRIMARY KEY,
    trip_id             TEXT NOT NULL,
    trip_start          TEXT NOT NULL,
    stop_index          INTEGER NOT NULL,
    eva                 INTEGER NOT NULL REFERENCES station_evas (eva),
    category            TEXT,
    train_number        TEXT,
    line                TEXT,
    planned_arrival     TEXT,
    planned_departure   TEXT,
    planned_platform    TEXT,
    changed_arrival     TEXT,
    changed_departure   TEXT,
    changed_platform    TEXT,
    arrival_cancelled   INTEGER NOT NULL DEFAULT 0 CHECK (arrival_cancelled IN (0, 1)),
    departure_cancelled INTEGER NOT NULL DEFAULT 0 CHECK (departure_cancelled IN (0, 1)),
    route_before        TEXT,
    route_after         TEXT,
    coupled_with        TEXT,
    first_seen_at       TEXT NOT NULL,
    last_updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS stops_by_trip ON stops (trip_id, trip_start);
CREATE INDEX IF NOT EXISTS stops_by_station_time ON stops (eva, planned_departure);

CREATE TABLE IF NOT EXISTS stop_changes (
    change_id           INTEGER PRIMARY KEY,
    stop_id             TEXT NOT NULL REFERENCES stops (stop_id),
    observed_at         TEXT NOT NULL,
    source              TEXT NOT NULL CHECK (source IN ('fchg', 'rchg')),
    changed_arrival     TEXT,
    changed_departure   TEXT,
    changed_platform    TEXT,
    arrival_cancelled   INTEGER NOT NULL DEFAULT 0 CHECK (arrival_cancelled IN (0, 1)),
    departure_cancelled INTEGER NOT NULL DEFAULT 0 CHECK (departure_cancelled IN (0, 1))
);
CREATE INDEX IF NOT EXISTS stop_changes_by_stop ON stop_changes (stop_id, observed_at);

CREATE TABLE IF NOT EXISTS stop_messages (
    stop_id      TEXT NOT NULL REFERENCES stops (stop_id),
    message_id   TEXT NOT NULL,
    type         TEXT,
    code         INTEGER,
    message_time TEXT,
    PRIMARY KEY (stop_id, message_id)
);
"""


def database_path() -> Path:
    """Returns where the database lives.

    Returns:
        The path in ``DB_ML_DATABASE_PATH`` if set, otherwise
        ``data/db_ml.sqlite``.
    """
    return Path(os.environ.get("DB_ML_DATABASE_PATH", DEFAULT_DATABASE_PATH))


def connect(path: Path | str) -> sqlite3.Connection:
    """Opens the database, creating the file and its folder if needed.

    Turns on WAL mode, so readers don't block the collector's writes, and
    foreign key checks, which SQLite leaves off by default.

    Args:
        path: The database file, or ``":memory:"`` for tests.

    Returns:
        An open connection whose rows can be read by column name.
    """
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Creates any tables and indexes that don't exist yet.

    Args:
        conn: An open database connection.
    """
    conn.executescript(SCHEMA)


def upsert_stations(conn: sqlite3.Connection, stations: list[dict[str, Any]]) -> None:
    """Inserts stations, or updates them if they already exist.

    Args:
        conn: An open database connection.
        stations: Records with ``stationNumber``, ``name``, ``city``,
            ``municipalityCode``, ``population``, ``latitude`` and
            ``longitude`` keys.
    """
    with conn:
        conn.executemany(
            """
            INSERT INTO stations (station_number, name, city, municipality_code,
                                  population, latitude, longitude)
            VALUES (:stationNumber, :name, :city, :municipalityCode,
                    :population, :latitude, :longitude)
            ON CONFLICT (station_number) DO UPDATE SET
                name = excluded.name,
                city = excluded.city,
                municipality_code = excluded.municipality_code,
                population = excluded.population,
                latitude = excluded.latitude,
                longitude = excluded.longitude
            """,
            stations,
        )


def sync_station_evas(conn: sqlite3.Connection, evas: list[dict[str, Any]]) -> None:
    """Makes the EVA numbers to collect match the given list exactly.

    Each EVA is inserted or updated. EVAs already in the database but missing
    from ``evas`` (e.g. a city that dropped out of the top 60) stop being
    collected. They are switched off rather than deleted, because collected
    stops may still refer to them.

    Args:
        conn: An open database connection.
        evas: Records with ``eva``, ``stationNumber``, ``timetableName`` and
            ``collect`` keys, as written by ``db_ml.station_evas``.
    """
    with conn:
        conn.executemany(
            """
            INSERT INTO station_evas (eva, station_number, timetable_name, collect)
            VALUES (:eva, :stationNumber, :timetableName, :collect)
            ON CONFLICT (eva) DO UPDATE SET
                station_number = excluded.station_number,
                timetable_name = excluded.timetable_name,
                collect = excluded.collect
            """,
            [{**e, "collect": int(e["collect"])} for e in evas],
        )
        placeholders = ", ".join("?" for _ in evas)
        conn.execute(
            f"UPDATE station_evas SET collect = 0 WHERE eva NOT IN ({placeholders})",
            [e["eva"] for e in evas],
        )


def collected_evas(conn: sqlite3.Connection) -> list[int]:
    """Lists the EVA numbers the collector should poll.

    Args:
        conn: An open database connection.

    Returns:
        EVA numbers marked for collection, in ascending order.
    """
    rows = conn.execute("SELECT eva FROM station_evas WHERE collect = 1 ORDER BY eva")
    return [row["eva"] for row in rows]


def main() -> None:
    """Creates the database and loads stations and their EVA numbers."""
    stations = load_json(TOP_STATIONS_PATH)
    path = database_path()
    conn = connect(path)
    init_db(conn)
    upsert_stations(conn, stations)
    sync_station_evas(conn, load_json(STATION_EVAS_PATH))
    print(
        f"Loaded {len(stations)} stations and {len(collected_evas(conn))} "
        f"EVA numbers to collect into {path}"
    )
    conn.close()


if __name__ == "__main__":
    main()
