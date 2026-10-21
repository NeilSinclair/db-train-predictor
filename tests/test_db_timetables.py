"""Tests for storing parsed timetable data (save_plan and save_changes).

Uses the real Frankfurt fixtures described in test_timetable_xml.py.
"""

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from db_ml.db import connect, init_db, save_changes, save_plan, sync_station_evas, upsert_stations
from db_ml.timetable_xml import BERLIN, parse_timetable

FIXTURES = Path(__file__).parent / "fixtures"
FETCHED_AT = datetime(2026, 10, 20, 20, 0, tzinfo=BERLIN)
FRANKFURT = 8000105
ICE_1554 = "3639185015027004194-2610201611-9"
ICE_1119 = "-5207450347899841717-2610201800-5"
ICE_729 = "-3019965818182152821-2610201821-4"
RE_4678 = "-2204298528222529683-2610201939-9"
S_35766 = "488578513775240193-2610201928-10"
RB_12 = "3203200605183773385-2610201947-1"
VIA_25169 = "5031310367914604275-2610201224-1"


def load(name: str) -> list:
    """Parses one of the Frankfurt fixtures.

    Args:
        name: The fixture's file name.

    Returns:
        The parsed stops.
    """
    return parse_timetable((FIXTURES / name).read_bytes(), FRANKFURT, FETCHED_AT)


PLAN = load("plan_frankfurt_2026-10-20_20.xml")
CHANGES = load("fchg_frankfurt_2026-10-20_2000.xml")


@pytest.fixture
def conn():
    """Provides a database that knows Frankfurt(Main)Hbf.

    Yields:
        An open SQLite connection.
    """
    connection = connect(":memory:")
    init_db(connection)
    upsert_stations(connection, [{
        "stationNumber": 1866, "name": "Frankfurt (Main) Hbf", "city": "Frankfurt am Main",
        "municipalityCode": "06412000", "population": 760_656,
        "latitude": 50.107145, "longitude": 8.663789,
    }])
    sync_station_evas(connection, [
        {"eva": FRANKFURT, "stationNumber": 1866, "timetableName": "Frankfurt(Main)Hbf", "collect": True},
    ])
    yield connection
    connection.close()


def stop_row(conn, stop_id: str):
    """Reads one row from stops.

    Args:
        conn: An open database connection.
        stop_id: The stop to read.

    Returns:
        The row, or None.
    """
    return conn.execute("SELECT * FROM stops WHERE stop_id = ?", (stop_id,)).fetchone()


def count(conn, table: str) -> int:
    """Counts the rows in a table.

    Args:
        conn: An open database connection.
        table: The table name.

    Returns:
        The number of rows.
    """
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_save_plan_stores_stops_and_skips_s_bahn(conn):
    result = save_plan(conn, PLAN, FETCHED_AT)

    assert (result.saved, result.skipped) == (7, 1)
    assert stop_row(conn, S_35766) is None
    row = stop_row(conn, ICE_1554)
    assert row["category"] == "ICE" and row["train_number"] == "1554"
    assert row["trip_start"] == "2026-10-20T14:11:00+00:00"
    assert row["planned_arrival"] == "2026-10-20T18:37:00+00:00"
    assert row["planned_departure"] == "2026-10-20T18:42:00+00:00"
    assert row["planned_platform"] == "5"
    assert row["changed_arrival"] is None
    assert row["first_seen_at"] == row["last_updated_at"] == "2026-10-20T18:00:00+00:00"


def test_save_plan_stores_coupled_trips(conn):
    save_plan(conn, PLAN, FETCHED_AT)

    row = stop_row(conn, "-6292402313742373016-2610202020-1")
    assert row["coupled_with"] == "1303617009360529856-2610202020"


def test_save_plan_skips_unknown_eva(conn):
    other = [replace(s, eva=999) for s in PLAN]

    assert save_plan(conn, other, FETCHED_AT).saved == 0


def test_save_changes_after_plan(conn):
    save_plan(conn, PLAN, FETCHED_AT)

    result = save_changes(conn, CHANGES, FETCHED_AT, "fchg")

    # 7 planned stops + 3 full-detail entries (ICE 729, ICE 2851, VIA 25169).
    # Skipped: S 35766 (not stored) and RB12 (unknown, no details).
    assert (result.saved, result.unchanged, result.skipped) == (10, 0, 2)
    assert count(conn, "stop_changes") == 10
    row = stop_row(conn, ICE_1554)
    assert row["changed_arrival"] == "2026-10-20T18:51:00+00:00"
    assert row["changed_departure"] == "2026-10-20T18:57:00+00:00"
    assert row["planned_arrival"] == "2026-10-20T18:37:00+00:00"  # untouched
    assert stop_row(conn, ICE_1119)["arrival_cancelled"] == 1
    assert stop_row(conn, RB_12) is None


def test_save_changes_creates_stop_from_full_detail_entry(conn):
    save_changes(conn, CHANGES, FETCHED_AT, "fchg")

    row = stop_row(conn, ICE_729)
    assert (row["category"], row["train_number"]) == ("ICE", "729")
    assert row["planned_arrival"] == "2026-10-20T17:51:00+00:00"
    assert row["changed_arrival"] == "2026-10-20T18:28:00+00:00"


def test_save_changes_skips_stops_without_plan_until_plan_arrives(conn):
    first = save_changes(conn, CHANGES, FETCHED_AT, "fchg")
    assert stop_row(conn, ICE_1554) is None

    save_plan(conn, PLAN, FETCHED_AT)
    second = save_changes(conn, CHANGES, FETCHED_AT + timedelta(hours=1), "fchg")

    assert first.saved == 3
    assert second.saved == 7  # the planned stops now get their first snapshot
    assert stop_row(conn, ICE_1554)["changed_arrival"] == "2026-10-20T18:51:00+00:00"


def test_save_changes_adds_a_row_only_when_state_changes(conn):
    save_plan(conn, PLAN, FETCHED_AT)
    save_changes(conn, CHANGES, FETCHED_AT, "fchg")

    again = save_changes(conn, CHANGES, FETCHED_AT + timedelta(minutes=2), "rchg")

    assert (again.saved, again.unchanged) == (0, 10)
    assert count(conn, "stop_changes") == 10


def test_save_changes_records_a_later_delay(conn):
    save_plan(conn, PLAN, FETCHED_AT)
    save_changes(conn, CHANGES, FETCHED_AT, "fchg")
    ice = next(s for s in CHANGES if s.stop_id == ICE_1554)
    later = replace(ice, arrival=replace(ice.arrival, changed_time=ice.arrival.changed_time + timedelta(minutes=5)))

    result = save_changes(conn, [later], FETCHED_AT + timedelta(minutes=3), "rchg")

    assert result.saved == 1
    history = conn.execute(
        "SELECT observed_at, source, changed_arrival FROM stop_changes WHERE stop_id = ? ORDER BY change_id",
        (ICE_1554,),
    ).fetchall()
    assert [tuple(r) for r in history] == [
        ("2026-10-20T18:00:00+00:00", "fchg", "2026-10-20T18:51:00+00:00"),
        ("2026-10-20T18:03:00+00:00", "rchg", "2026-10-20T18:56:00+00:00"),
    ]
    assert stop_row(conn, ICE_1554)["changed_arrival"] == "2026-10-20T18:56:00+00:00"


def test_save_changes_stores_cancellation_time_operator_and_changed_path(conn):
    save_plan(conn, PLAN, FETCHED_AT)
    save_changes(conn, CHANGES, FETCHED_AT, "fchg")

    assert stop_row(conn, ICE_1554)["operator"] == "80"  # from /plan
    ice_1119 = stop_row(conn, ICE_1119)
    assert ice_1119["arrival_cancelled_at"] == "2026-10-20T12:43:00+00:00"
    assert ice_1119["changed_route_before"] == ""
    snapshot = conn.execute(
        "SELECT arrival_cancelled_at FROM stop_changes WHERE stop_id = ?", (ICE_1119,)
    ).fetchone()
    assert snapshot[0] == "2026-10-20T12:43:00+00:00"
    via = stop_row(conn, VIA_25169)  # created from a full-detail change entry
    assert (via["operator"], via["category"]) == ("R4", "VIA")


def test_save_changes_records_a_diversion(conn):
    save_plan(conn, PLAN, FETCHED_AT)
    save_changes(conn, CHANGES, FETCHED_AT, "fchg")
    ice = next(s for s in CHANGES if s.stop_id == ICE_1554)
    diverted = replace(ice, departure=replace(ice.departure, changed_path="Hanau Hbf|Fulda"))

    result = save_changes(conn, [diverted], FETCHED_AT + timedelta(minutes=3), "rchg")

    assert result.saved == 1
    assert stop_row(conn, ICE_1554)["changed_route_after"] == "Hanau Hbf|Fulda"
    assert count(conn, "stop_changes") == 11


def test_save_changes_records_reinstated_train(conn):
    save_plan(conn, PLAN, FETCHED_AT)
    save_changes(conn, CHANGES, FETCHED_AT, "fchg")
    ice = next(s for s in CHANGES if s.stop_id == ICE_1119)
    reinstated = replace(ice, arrival=replace(ice.arrival, cancelled=False))

    save_changes(conn, [reinstated], FETCHED_AT + timedelta(minutes=5), "rchg")

    assert stop_row(conn, ICE_1119)["arrival_cancelled"] == 0
    assert count(conn, "stop_changes") == 11


def test_save_changes_stores_messages_once(conn):
    save_plan(conn, PLAN, FETCHED_AT)
    save_changes(conn, CHANGES, FETCHED_AT, "fchg")
    before = count(conn, "stop_messages")

    save_changes(conn, CHANGES, FETCHED_AT + timedelta(minutes=2), "rchg")

    assert count(conn, "stop_messages") == before > 0
    notice = conn.execute(
        "SELECT type, code, category, valid_from, valid_to, priority FROM stop_messages "
        "WHERE stop_id = ? AND type = 'h'",
        (RE_4678,),
    ).fetchone()
    assert tuple(notice) == (
        "h", None, "BAUARBEITEN", "2026-10-14T07:28:00+00:00", "2026-10-25T21:00:00+00:00", 3,
    )


def test_save_plan_again_keeps_changed_values(conn):
    save_plan(conn, PLAN, FETCHED_AT)
    save_changes(conn, CHANGES, FETCHED_AT, "fchg")

    save_plan(conn, PLAN, FETCHED_AT + timedelta(hours=1))

    row = stop_row(conn, ICE_1554)
    assert row["changed_arrival"] == "2026-10-20T18:51:00+00:00"
    assert row["first_seen_at"] == "2026-10-20T18:00:00+00:00"
    assert row["last_updated_at"] == "2026-10-20T19:00:00+00:00"


def test_stored_times_sort_in_time_order_across_the_clock_change(conn):
    # 02:45 CEST (00:45 UTC) is earlier than 02:15 CET (01:15 UTC), although
    # the local text sorts the other way.
    xml = """<timetable station='X'>
      <s id="1-2610250100-2"><tl c="ICE" n="1"/><ar pt="2610250245"/></s>
      <s id="2-2610250100-2"><tl c="ICE" n="2"/><ar pt="2610250215" ct="2610250215"/></s>
    </timetable>"""
    fetched = datetime(2026, 10, 25, 1, 0, tzinfo=BERLIN)
    stops = parse_timetable(xml, FRANKFURT, fetched)
    # Pin the second stop's arrival to the second 02:15 (CET).
    second = replace(stops[1], arrival=replace(
        stops[1].arrival, planned_time=datetime(2026, 10, 25, 2, 15, fold=1, tzinfo=BERLIN),
    ))

    save_plan(conn, [stops[0], second], fetched)

    order = [r[0] for r in conn.execute("SELECT train_number FROM stops ORDER BY planned_arrival")]
    assert order == ["1", "2"]
    assert conn.execute("SELECT planned_arrival FROM stops WHERE train_number = '2'").fetchone()[0] == (
        "2026-10-25T01:15:00+00:00"
    )
