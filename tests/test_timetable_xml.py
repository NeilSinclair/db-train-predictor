"""Tests for parsing Timetables API XML.

The fixtures are real responses for Frankfurt(Main)Hbf (EVA 8000105),
fetched 2026-10-20 at 20:00, trimmed to a few stops that cover each case.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from db_ml.timetable_xml import BERLIN, parse_db_time, parse_timetable, split_stop_id

FIXTURES = Path(__file__).parent / "fixtures"
FETCHED_AT = datetime(2026, 10, 20, 20, 0, tzinfo=BERLIN)
FRANKFURT = 8000105


def berlin(*args: int) -> datetime:
    """Builds a Europe/Berlin datetime.

    Args:
        *args: Year, month, day, hour, minute.

    Returns:
        An aware datetime.
    """
    return datetime(*args, tzinfo=BERLIN)


@pytest.fixture(scope="module")
def plan():
    """Parses the /plan fixture.

    Returns:
        A dict of parsed stops keyed by train, e.g. ``"ICE 1554"``.
    """
    content = (FIXTURES / "plan_frankfurt_2026-10-20_20.xml").read_bytes()
    return {f"{s.category} {s.train_number}": s for s in parse_timetable(content, FRANKFURT, FETCHED_AT)}


@pytest.fixture(scope="module")
def changes():
    """Parses the /fchg fixture.

    Returns:
        A dict of parsed stops keyed by stop ID.
    """
    content = (FIXTURES / "fchg_frankfurt_2026-10-20_2000.xml").read_bytes()
    return {s.stop_id: s for s in parse_timetable(content, FRANKFURT, FETCHED_AT)}


def test_parse_db_time_summer_and_winter_offsets():
    assert parse_db_time("2610201718", FETCHED_AT).isoformat() == "2026-10-20T17:18:00+02:00"
    assert parse_db_time("2611011200", FETCHED_AT).isoformat() == "2026-11-01T12:00:00+01:00"


@pytest.mark.parametrize("text", [None, ""])
def test_parse_db_time_empty(text):
    assert parse_db_time(text, FETCHED_AT) is None


def test_parse_db_time_resolves_the_repeated_hour_on_25_october():
    # Clocks go back at 03:00 CEST to 02:00 CET, so 02:30 happens twice.
    before_change = datetime(2026, 10, 24, 23, 50, tzinfo=UTC)  # 01:50 CEST
    after_change = datetime(2026, 10, 25, 1, 50, tzinfo=UTC)    # 02:50 CET

    first = parse_db_time("2610250230", before_change)
    second = parse_db_time("2610250230", after_change)

    assert first.isoformat() == "2026-10-25T02:30:00+02:00"
    assert second.isoformat() == "2026-10-25T02:30:00+01:00"
    assert (second.astimezone(UTC) - first.astimezone(UTC)).total_seconds() == 3600


def test_parse_db_time_hours_next_to_the_change_are_unambiguous():
    assert parse_db_time("2610250159", FETCHED_AT).isoformat() == "2026-10-25T01:59:00+02:00"
    assert parse_db_time("2610250300", FETCHED_AT).isoformat() == "2026-10-25T03:00:00+01:00"


def test_split_stop_id_handles_negative_trip_ids():
    assert split_stop_id("-2041012609203177085-2610200804-18") == (
        "-2041012609203177085", "2610200804", 18,
    )
    assert split_stop_id("488578513775240193-2610201928-10") == (
        "488578513775240193", "2610201928", 10,
    )


def test_plan_stop_through_train(plan):
    ice = plan["ICE 1554"]

    assert ice.stop_id == "3639185015027004194-2610201611-9"
    assert ice.trip_id == "3639185015027004194"
    assert ice.trip_start == berlin(2026, 10, 20, 16, 11)
    assert ice.stop_index == 9
    assert ice.eva == FRANKFURT  # /plan doesn't name the EVA, so the request's is used
    assert ice.arrival.planned_time == berlin(2026, 10, 20, 20, 37)
    assert ice.arrival.planned_platform == "5"
    assert ice.departure.planned_time == berlin(2026, 10, 20, 20, 42)
    assert ice.arrival.planned_path.split("|")[0] == "Dresden Hbf"
    assert ice.arrival.changed_time is None
    assert ice.arrival.cancelled_at is None and ice.arrival.changed_path is None
    assert ice.operator == "80"  # DB Fernverkehr
    assert ice.has_train_details and not ice.is_s_bahn


def test_plan_stop_first_and_last_stops(plan):
    # Starts here: departure only. Ends here: arrival only.
    assert plan["RE 4681"].arrival is None
    assert plan["ICE 1119"].departure is None


def test_plan_coupled_train_and_line(plan):
    hlb = plan["HLB 15120"]

    assert hlb.departure.wings == "1303617009360529856-2610202020"
    assert hlb.departure.line == "RE99"


def test_plan_s_bahn_is_flagged(plan):
    assert plan["S 35766"].is_s_bahn


def test_changes_delay_and_platform(changes):
    ice_1554 = changes["3639185015027004194-2610201611-9"]
    ice_379 = changes["-8394333759835531528-2610201643-6"]

    assert ice_1554.arrival.changed_time == berlin(2026, 10, 20, 20, 51)
    assert ice_1554.departure.changed_time == berlin(2026, 10, 20, 20, 57)
    assert not ice_1554.has_train_details  # change feeds usually leave out tl
    assert ice_379.departure.changed_platform == "9"


def test_changes_cancellation(changes):
    ice_1119 = changes["-5207450347899841717-2610201800-5"]

    assert ice_1119.arrival.cancelled
    assert ice_1119.arrival.changed_time is None


def test_changes_cancellation_time_and_changed_path(changes):
    ice_1119 = changes["-5207450347899841717-2610201800-5"]
    rb_12 = changes["3203200605183773385-2610201947-1"]

    assert ice_1119.arrival.cancelled_at == berlin(2026, 10, 20, 14, 43)
    assert ice_1119.arrival.changed_path == ""  # sent empty, not left out
    assert rb_12.departure.cancelled_at == berlin(2026, 10, 20, 19, 30)
    path = rb_12.departure.changed_path.split("|")
    assert (path[0], path[-1]) == ("Frankfurt-Höchst", "Königstein(Taunus)")


def test_changes_operator_from_full_detail_entry(changes):
    assert changes["-3019965818182152821-2610201821-4"].operator == "80"
    assert changes["5031310367914604275-2610201224-1"].operator == "R4"


def test_changes_reinstated_event_is_not_cancelled():
    xml = """<timetable station='X' eva='8000105'>
      <s id="1-2610201000-2"><ar ct="2610201005" cs="p" clt="2610200900"/></s>
    </timetable>"""

    [stop] = parse_timetable(xml, FRANKFURT, FETCHED_AT)

    assert not stop.arrival.cancelled


def test_changes_messages_are_unique_and_keep_category(changes):
    re_4678 = changes["-2204298528222529683-2610201939-9"]
    ice_1554 = changes["3639185015027004194-2610201611-9"]

    [notice] = [m for m in re_4678.messages if m.type == "h"]
    assert notice.category == "BAUARBEITEN"
    assert notice.code is None
    assert notice.valid_from == berlin(2026, 10, 14, 9, 28)
    assert notice.valid_to == berlin(2026, 10, 25, 22, 0)  # after the clock change: CET
    assert notice.valid_to.utcoffset().total_seconds() == 3600
    assert notice.priority == 3
    ids = [m.message_id for m in ice_1554.messages]
    assert len(ids) == len(set(ids)) > 0


def test_changes_delay_message_code(changes):
    ice_729 = changes["-3019965818182152821-2610201821-4"]

    codes = {m.code for m in ice_729.messages if m.type == "d"}
    assert codes == {36}


def test_changes_full_detail_entry(changes):
    ice_729 = changes["-3019965818182152821-2610201821-4"]

    assert ice_729.has_train_details
    assert (ice_729.category, ice_729.train_number) == ("ICE", "729")
    assert ice_729.arrival.planned_time == berlin(2026, 10, 20, 19, 51)
    assert ice_729.arrival.changed_time == berlin(2026, 10, 20, 20, 28)


def test_eva_comes_from_the_xml_when_present(changes):
    content = (FIXTURES / "fchg_frankfurt_2026-10-20_2000.xml").read_bytes()

    stops = parse_timetable(content, eva=1, fetched_at=FETCHED_AT)

    assert {s.eva for s in stops} == {FRANKFURT}


# The night of 25 Oct 2026: clocks go back at 03:00 CEST to 02:00 CET.
NIGHT_FETCH = datetime(2026, 10, 25, 0, 40, tzinfo=BERLIN)  # 00:40 CEST


def one_stop(inner: str, stop_id: str = "1-2610250100-3") -> str:
    """Wraps ar/dp elements in a minimal change-feed response.

    Args:
        inner: The XML inside the ``<s>`` element.
        stop_id: The stop ID.

    Returns:
        XML text for a timetable with one stop at EVA 8000105.
    """
    return f"<timetable station='X' eva='8000105'><s id=\"{stop_id}\">{inner}</s></timetable>"


def test_forecast_in_repeated_hour_is_not_read_as_running_early():
    # Planned 02:50 (first occurrence), forecast "02:30". Read as the first
    # 02:30 the train would be 20 min early; as the second, 40 min late.
    [stop] = parse_timetable(one_stop('<ar pt="2610250250" ct="2610250230"/>'), FRANKFURT, NIGHT_FETCH)

    assert stop.arrival.planned_time.isoformat() == "2026-10-25T02:50:00+02:00"
    assert stop.arrival.changed_time.isoformat() == "2026-10-25T02:30:00+01:00"


def test_forecast_in_repeated_hour_prefers_the_smaller_delay():
    # Planned 01:50 CEST, forecast "02:10": 20 min or 80 min late. Without
    # other information the smaller delay is the likelier one.
    [stop] = parse_timetable(one_stop('<dp pt="2610250150" ct="2610250210"/>'), FRANKFURT, NIGHT_FETCH)

    delay = stop.departure.changed_time.astimezone(UTC) - stop.departure.planned_time.astimezone(UTC)
    assert delay.total_seconds() == 20 * 60


def test_departure_in_repeated_hour_is_not_before_its_arrival():
    # Arrives 02:55 (first occurrence), departs "02:02" after a 7-minute stop:
    # only the second 02:02 comes after the arrival.
    [stop] = parse_timetable(one_stop('<ar pt="2610250255"/><dp pt="2610250202"/>'), FRANKFURT, NIGHT_FETCH)

    assert stop.arrival.planned_time.isoformat() == "2026-10-25T02:55:00+02:00"
    assert stop.departure.planned_time.isoformat() == "2026-10-25T02:02:00+01:00"


def test_trip_start_in_repeated_hour_is_the_same_at_every_station():
    # Different stations see the trip at different times; journeys are
    # grouped on trip start, so it must resolve identically.
    early = parse_timetable(one_stop('<ar pt="2610250245"/>', "7-2610250230-2"), FRANKFURT, NIGHT_FETCH)
    late = parse_timetable(
        one_stop('<ar pt="2610250530"/>', "7-2610250230-6"), FRANKFURT, datetime(2026, 10, 25, 5, tzinfo=BERLIN)
    )

    assert early[0].trip_start == late[0].trip_start
    assert early[0].trip_start.isoformat() == "2026-10-25T02:30:00+02:00"


def test_time_skipped_when_clocks_go_forward_is_moved_to_the_real_time():
    # 29 Mar 2026: 02:00 CET jumps to 03:00 CEST, so 02:30 never happens.
    result = parse_db_time("2603290230", FETCHED_AT)

    assert result.isoformat() == "2026-03-29T03:30:00+02:00"


def test_malformed_stop_is_skipped_and_the_rest_kept(caplog):
    xml = """<timetable station='X' eva='8000105'>
      <s id="not-a-valid-id"><ar pt="2610201000"/></s>
      <s id="1-2610200900-2"><ar pt="not-a-time"/></s>
      <s id="2-2610200900-3"><ar pt="2610201005"/></s>
    </timetable>"""

    stops = parse_timetable(xml, FRANKFURT, FETCHED_AT)

    assert [s.stop_id for s in stops] == ["2-2610200900-3"]
    assert caplog.text.count("Skipping malformed stop") == 2


def test_unusual_message_code_is_ignored_not_fatal():
    [stop] = parse_timetable(
        one_stop('<ar pt="2610201000"><m id="m1" t="d" c="--5" ts="2610200900"/></ar>'), FRANKFURT, FETCHED_AT
    )

    assert stop.messages[0].code is None
