"""SQLite storage for stations and collected timetable data.

All SQL lives in this module (decision D11), so a later move to Postgres
stays contained. The schema follows decision D7. The ``journeys`` table is
not created here because it's derived from ``stops`` by a later job.

Times are stored as ISO 8601 text in UTC, e.g. ``2026-10-20T15:18:00+00:00``
for 17:18 German summer time. Keeping one offset means sorting or comparing
the text gives time order, also across the clock change (D14). Convert to
Europe/Berlin when reading for display or for local-time features.
Booleans are stored as 0 or 1.

Example:
    Create the database and load the reference data::

        $ uv run python -m db_ml.db
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from db_ml.files import load_json
from db_ml.timetable_xml import Event, Stop

DEFAULT_DATABASE_PATH = Path("data/db_ml.sqlite")
TOP_STATIONS_PATH = Path("data/processed/top_stations.json")
STATION_EVAS_PATH = Path("data/processed/station_evas.json")

# Version 1 is the schema as it stood before the first deployment. Its
# IF NOT EXISTS clauses let databases created before migrations existed
# (user_version 0) be marked as version 1 without changes.
SCHEMA_V1 = """
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
    category     TEXT,
    message_time TEXT,
    PRIMARY KEY (stop_id, message_id)
);
"""

# Which /plan hours have been fetched, so a restarted collector only fetches
# the ones it missed (D18). hour_label is the German local hour as in the
# /plan URL (YYMMDDHH), which is also how the collector tells hours apart.
SCHEMA_V2 = """
CREATE TABLE plan_fetches (
    eva        INTEGER NOT NULL REFERENCES station_evas (eva),
    hour_label TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (eva, hour_label)
);
"""

# Attributes the model features need that DB doesn't keep for us (D23,
# docs/features.md): the operator, when a cancellation was announced, the
# changed path of a diverted train, and when disruption notices (e.g.
# construction work) apply. Existing rows get NULL.
SCHEMA_V3 = """
ALTER TABLE stops ADD COLUMN operator TEXT;
ALTER TABLE stops ADD COLUMN arrival_cancelled_at TEXT;
ALTER TABLE stops ADD COLUMN departure_cancelled_at TEXT;
ALTER TABLE stops ADD COLUMN changed_route_before TEXT;
ALTER TABLE stops ADD COLUMN changed_route_after TEXT;
ALTER TABLE stop_changes ADD COLUMN arrival_cancelled_at TEXT;
ALTER TABLE stop_changes ADD COLUMN departure_cancelled_at TEXT;
ALTER TABLE stop_changes ADD COLUMN changed_route_before TEXT;
ALTER TABLE stop_changes ADD COLUMN changed_route_after TEXT;
ALTER TABLE stop_messages ADD COLUMN valid_from TEXT;
ALTER TABLE stop_messages ADD COLUMN valid_to TEXT;
ALTER TABLE stop_messages ADD COLUMN priority INTEGER;
"""

# Schema migrations in order: entry N takes the database from version N to
# N + 1, tracked in SQLite's ``PRAGMA user_version``. Only ever append; never
# edit a migration that has run on a real database.
MIGRATIONS = [SCHEMA_V1, SCHEMA_V2, SCHEMA_V3]


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


def schema_version(conn: sqlite3.Connection) -> int:
    """Reads which migration the database is at.

    Args:
        conn: An open database connection.

    Returns:
        The database's ``user_version``: 0 for a new database, otherwise
        the number of migrations applied.
    """
    return conn.execute("PRAGMA user_version").fetchone()[0]


def init_db(conn: sqlite3.Connection, migrations: list[str] = MIGRATIONS) -> None:
    """Brings the schema up to date by applying any pending migrations.

    Each migration runs in its own transaction together with the version
    bump, so a failing migration leaves the database at the version before.

    Args:
        conn: An open database connection.
        migrations: The migration scripts, in order. Tests pass their own.

    Raises:
        RuntimeError: If the database is newer than this code, e.g. after
            rolling back a deployment.
        sqlite3.Error: If a migration fails; it is rolled back.
    """
    version = schema_version(conn)
    if version > len(migrations):
        raise RuntimeError(
            f"Database is at schema version {version}, but this code only knows {len(migrations)}"
        )
    for number, script in enumerate(migrations[version:], start=version + 1):
        try:
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
        except sqlite3.Error:
            conn.rollback()
            raise


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


@dataclass(frozen=True)
class SaveResult:
    """How many stops a save call handled, by outcome.

    Attributes:
        saved: Stops written (for change feeds: stops whose state changed).
        unchanged: Change-feed stops whose state matched the last snapshot.
        skipped: Stops not stored: S-Bahn trains, EVAs we don't know, and
            changes for stops we have no details for yet.
    """

    saved: int = 0
    unchanged: int = 0
    skipped: int = 0


def _iso(value: datetime | None) -> str | None:
    """Formats a time for storage, in UTC.

    Args:
        value: An aware datetime, or None.

    Returns:
        ISO 8601 text in UTC (``+00:00``), or None.
    """
    return value.astimezone(UTC).isoformat() if value else None


UPSERT_STOP = """
INSERT INTO stops (stop_id, trip_id, trip_start, stop_index, eva, category,
                   train_number, operator, line, planned_arrival,
                   planned_departure, planned_platform, route_before,
                   route_after, coupled_with, first_seen_at, last_updated_at)
VALUES (:stop_id, :trip_id, :trip_start, :stop_index, :eva, :category,
        :train_number, :operator, :line, :planned_arrival,
        :planned_departure, :planned_platform, :route_before,
        :route_after, :coupled_with, :now, :now)
ON CONFLICT (stop_id) DO UPDATE SET
    category = COALESCE(excluded.category, stops.category),
    train_number = COALESCE(excluded.train_number, stops.train_number),
    operator = COALESCE(excluded.operator, stops.operator),
    line = COALESCE(excluded.line, stops.line),
    planned_arrival = COALESCE(excluded.planned_arrival, stops.planned_arrival),
    planned_departure = COALESCE(excluded.planned_departure, stops.planned_departure),
    planned_platform = COALESCE(excluded.planned_platform, stops.planned_platform),
    route_before = COALESCE(excluded.route_before, stops.route_before),
    route_after = COALESCE(excluded.route_after, stops.route_after),
    coupled_with = COALESCE(excluded.coupled_with, stops.coupled_with),
    last_updated_at = excluded.last_updated_at
"""


def _planned_row(stop: Stop, observed_at: datetime) -> dict[str, Any]:
    """Builds the planned part of a ``stops`` row.

    Args:
        stop: A parsed stop.
        observed_at: When the data was fetched.

    Returns:
        Named parameters for ``UPSERT_STOP``.
    """
    arrival, departure = stop.arrival or Event(), stop.departure or Event()
    return {
        "stop_id": stop.stop_id,
        "trip_id": stop.trip_id,
        "trip_start": _iso(stop.trip_start),
        "stop_index": stop.stop_index,
        "eva": stop.eva,
        "category": stop.category,
        "train_number": stop.train_number,
        "operator": stop.operator,
        "line": departure.line or arrival.line,
        "planned_arrival": _iso(arrival.planned_time),
        "planned_departure": _iso(departure.planned_time),
        # One platform column (D7): the departure's, else the arrival's.
        "planned_platform": departure.planned_platform or arrival.planned_platform,
        "route_before": arrival.planned_path,
        "route_after": departure.planned_path,
        "coupled_with": departure.wings or arrival.wings,
        "now": _iso(observed_at),
    }


# The changeable state of a stop, in the order ``_snapshot`` returns it. These
# are the column names in both ``stop_changes`` and ``stops``.
SNAPSHOT_COLUMNS = (
    "changed_arrival", "changed_departure", "changed_platform",
    "arrival_cancelled", "departure_cancelled",
    "arrival_cancelled_at", "departure_cancelled_at",
    "changed_route_before", "changed_route_after",
)


def _snapshot(stop: Stop) -> tuple:
    """Extracts the changeable state of a stop, as stored in ``stop_changes``.

    Args:
        stop: A stop parsed from a change feed.

    Returns:
        The values for ``SNAPSHOT_COLUMNS``, in that order.
    """
    arrival, departure = stop.arrival or Event(), stop.departure or Event()
    return (
        _iso(arrival.changed_time),
        _iso(departure.changed_time),
        departure.changed_platform or arrival.changed_platform,
        int(arrival.cancelled),
        int(departure.cancelled),
        _iso(arrival.cancelled_at),
        _iso(departure.cancelled_at),
        arrival.changed_path,
        departure.changed_path,
    )


def _known_evas(conn: sqlite3.Connection) -> set[int]:
    """Lists the EVA numbers in ``station_evas``.

    Args:
        conn: An open database connection.

    Returns:
        All EVA numbers stops may refer to.
    """
    return {row["eva"] for row in conn.execute("SELECT eva FROM station_evas")}


def save_plan(conn: sqlite3.Connection, stops: list[Stop], observed_at: datetime) -> SaveResult:
    """Stores stops from a ``/plan`` response.

    New stops are inserted. For stops already stored, planned values are
    updated and changed values are left alone. S-Bahn trains are skipped (D8).

    Args:
        conn: An open database connection.
        stops: Stops parsed from a ``/plan`` response.
        observed_at: When the response was fetched.

    Returns:
        How many stops were saved and skipped.
    """
    known = _known_evas(conn)
    rows = [_planned_row(s, observed_at) for s in stops if not s.is_s_bahn and s.eva in known]
    with conn:
        conn.executemany(UPSERT_STOP, rows)
    return SaveResult(saved=len(rows), skipped=len(stops) - len(rows))


def record_plan_fetch(
    conn: sqlite3.Connection, eva: int, hour_label: str, fetched_at: datetime
) -> None:
    """Notes that a ``/plan`` hour has been fetched.

    Args:
        conn: An open database connection.
        eva: The EVA number.
        hour_label: The German local hour as ``YYMMDDHH``.
        fetched_at: When it was fetched. A later fetch of the same hour
            overwrites it.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO plan_fetches (eva, hour_label, fetched_at) VALUES (?, ?, ?)
            ON CONFLICT (eva, hour_label) DO UPDATE SET fetched_at = excluded.fetched_at
            """,
            (eva, hour_label, _iso(fetched_at)),
        )


def fetched_plan_hours(conn: sqlite3.Connection, hour_labels: set[str]) -> set[tuple[int, str]]:
    """Looks up which of the given ``/plan`` hours have been fetched.

    Args:
        conn: An open database connection.
        hour_labels: German local hours as ``YYMMDDHH``.

    Returns:
        (EVA, hour label) pairs fetched before, for any EVA.
    """
    labels = sorted(hour_labels)
    placeholders = ", ".join("?" for _ in labels)
    rows = conn.execute(
        f"SELECT eva, hour_label FROM plan_fetches WHERE hour_label IN ({placeholders})", labels
    )
    return {(row["eva"], row["hour_label"]) for row in rows}


def _last_snapshot(conn: sqlite3.Connection, stop_id: str) -> tuple | None:
    """Reads the most recent stored snapshot for a stop.

    Args:
        conn: An open database connection.
        stop_id: The stop to look up.

    Returns:
        The snapshot tuple in ``_snapshot`` order, or None if there's none.
    """
    row = conn.execute(
        f"SELECT {', '.join(SNAPSHOT_COLUMNS)} FROM stop_changes "
        "WHERE stop_id = ? ORDER BY change_id DESC LIMIT 1",
        (stop_id,),
    ).fetchone()
    return tuple(row) if row else None


INSERT_SNAPSHOT = (
    f"INSERT INTO stop_changes (stop_id, observed_at, source, {', '.join(SNAPSHOT_COLUMNS)}) "
    f"VALUES (?, ?, ?, {', '.join('?' for _ in SNAPSHOT_COLUMNS)})"
)
UPDATE_STOP_STATE = (
    f"UPDATE stops SET {', '.join(f'{c} = ?' for c in SNAPSHOT_COLUMNS)}, last_updated_at = ? "
    "WHERE stop_id = ?"
)


def save_changes(
    conn: sqlite3.Connection, stops: list[Stop], observed_at: datetime, source: str
) -> SaveResult:
    """Stores stops from an ``/fchg`` or ``/rchg`` response.

    Each change-feed entry is the stop's full current change state. A new
    ``stop_changes`` row is added only when that state differs from the last
    one stored (D9), and the stop's ``changed_*`` columns are updated to
    match. Messages are added once per stop.

    A change for a stop we don't have yet is kept only if the entry names
    the train (e.g. one added at short notice); then the stop is created
    from it. Other unknown stops are skipped: they're S-Bahn trains, or
    trains whose ``/plan`` hour we haven't fetched yet, and ``/fchg`` will
    report them again once we have.

    Args:
        conn: An open database connection.
        stops: Stops parsed from a change feed.
        observed_at: When the response was fetched.
        source: ``"fchg"`` or ``"rchg"``.

    Returns:
        How many stops changed, were unchanged and were skipped.
    """
    known = _known_evas(conn)
    saved = unchanged = skipped = 0
    now = _iso(observed_at)
    with conn:
        for stop in stops:
            if stop.is_s_bahn or stop.eva not in known:
                skipped += 1
                continue
            if stop.has_train_details:
                conn.execute(UPSERT_STOP, _planned_row(stop, observed_at))
            elif not conn.execute("SELECT 1 FROM stops WHERE stop_id = ?", (stop.stop_id,)).fetchone():
                skipped += 1
                continue

            snapshot = _snapshot(stop)
            if snapshot == _last_snapshot(conn, stop.stop_id):
                unchanged += 1
            else:
                saved += 1
                conn.execute(INSERT_SNAPSHOT, (stop.stop_id, now, source, *snapshot))
                conn.execute(UPDATE_STOP_STATE, (*snapshot, now, stop.stop_id))
            conn.executemany(
                """
                INSERT OR IGNORE INTO stop_messages (stop_id, message_id, type,
                    code, category, message_time, valid_from, valid_to, priority)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (stop.stop_id, m.message_id, m.type, m.code, m.category, _iso(m.time),
                     _iso(m.valid_from), _iso(m.valid_to), m.priority)
                    for m in stop.messages
                ],
            )
    return SaveResult(saved=saved, unchanged=unchanged, skipped=skipped)


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
