"""Tests for the collector's scheduling, rate limiting and storage."""

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from db_ml.api import ApiConfig
from db_ml import api as api_module
from db_ml.collector import (
    PLAN_HOURS_AHEAD,
    PLAN_HOURS_BACK,
    PLAN_RETRY_DELAY,
    RCHG_INTERVAL,
    Collector,
    Kind,
    RateLimiter,
    floor_hour,
    interruptible_sleep,
    local_hour_label,
)
from db_ml.db import connect, init_db, sync_station_evas, upsert_stations
from db_ml.timetables import build_client

FIXTURES = Path(__file__).parent / "fixtures"
FRANKFURT = 8000105
BERLIN_HBF = 8098160
# 20:00 in Frankfurt on 2026-10-20, when the fixtures were fetched.
START = datetime(2026, 10, 20, 18, 0, tzinfo=UTC)
PLAN_HOURS = PLAN_HOURS_AHEAD + 1 + PLAN_HOURS_BACK


class FakeClock:
    """A clock that only moves when told to.

    Attributes:
        current: The current time.
    """

    def __init__(self, start: datetime) -> None:
        """Starts the clock.

        Args:
            start: The initial time.
        """
        self.current = start

    def now(self) -> datetime:
        """Returns the current time.

        Returns:
            The current time.
        """
        return self.current

    def sleep(self, seconds: float) -> None:
        """Moves the clock forward instead of waiting.

        Args:
            seconds: How far to move.
        """
        self.current += timedelta(seconds=seconds)


class FakeApi:
    """Serves the Frankfurt fixtures and records requests.

    Attributes:
        paths: The path of every request received.
        fail: Paths that should get this status code instead of data.
    """

    def __init__(self) -> None:
        """Creates the fake API."""
        self.paths: list[str] = []
        self.fail: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answers one request.

        Args:
            request: The incoming request.

        Returns:
            The fixture for Frankfurt's 20:00 plan and its changes, an
            empty timetable otherwise.
        """
        path = request.url.path.removeprefix("/timetables/v1")
        self.paths.append(path)
        if path in self.fail:
            return httpx.Response(self.fail[path])
        if path == f"/plan/{FRANKFURT}/261020/20":
            return httpx.Response(200, content=(FIXTURES / "plan_frankfurt_2026-10-20_20.xml").read_bytes())
        if path == f"/fchg/{FRANKFURT}":
            return httpx.Response(200, content=(FIXTURES / "fchg_frankfurt_2026-10-20_2000.xml").read_bytes())
        return httpx.Response(200, text="<timetable station='X'/>")


@pytest.fixture
def conn():
    """Provides a database that knows Frankfurt and Berlin Hbf.

    Yields:
        An open SQLite connection.
    """
    connection = connect(":memory:")
    init_db(connection)
    upsert_stations(connection, [
        {"stationNumber": 1866, "name": "Frankfurt (Main) Hbf", "city": "Frankfurt am Main",
         "municipalityCode": "06412000", "population": 760_656, "latitude": 50.1, "longitude": 8.7},
        {"stationNumber": 1071, "name": "Berlin Hauptbahnhof", "city": "Berlin",
         "municipalityCode": "11000000", "population": 3_700_577, "latitude": 52.5, "longitude": 13.4},
    ])
    sync_station_evas(connection, [
        {"eva": FRANKFURT, "stationNumber": 1866, "timetableName": "Frankfurt(Main)Hbf", "collect": True},
        {"eva": BERLIN_HBF, "stationNumber": 1071, "timetableName": "Berlin Hbf", "collect": True},
    ])
    yield connection
    connection.close()


@pytest.fixture
def api():
    """Provides the fake Timetables API.

    Returns:
        A FakeApi.
    """
    return FakeApi()


@pytest.fixture
def clock():
    """Provides a fake clock at 20:00 German time on 2026-10-20.

    Returns:
        A FakeClock.
    """
    return FakeClock(START)


def make_collector(conn, api, clock, evas=(FRANKFURT, BERLIN_HBF), stop=None) -> Collector:
    """Builds a started collector wired to the fakes.

    Args:
        conn: The database connection.
        api: The fake API.
        clock: The fake clock.
        evas: The EVA numbers to collect.
        stop: Optional stop event.

    Returns:
        A Collector with its startup tasks queued.
    """
    client = build_client(ApiConfig("id", "key", "https://example.test/timetables/v1"), httpx.MockTransport(api.handler))
    collector = Collector(conn, client, list(evas), now=clock.now, sleep=clock.sleep, stop=stop)
    collector.start()
    return collector


def run_tasks(collector: Collector, clock: FakeClock, count: int) -> list:
    """Runs a number of tasks, letting time pass when nothing is due.

    Args:
        collector: The collector.
        clock: The fake clock.
        count: How many tasks to run.

    Returns:
        The tasks that ran, in order.
    """
    ran = []
    while len(ran) < count:
        task = collector.run_next()
        if task is None:
            clock.sleep(collector.seconds_until_next_task(clock.now()) or 1)
        else:
            ran.append(task)
    return ran


def test_rate_limiter_spaces_calls():
    t = [100.0]
    sleeps: list[float] = []

    def sleep(seconds):
        """Records the wait and moves the fake clock.

        Args:
            seconds: How long the limiter asked to wait.
        """
        sleeps.append(seconds)
        t[0] += seconds

    limiter = RateLimiter(1.05, monotonic=lambda: t[0], sleep=sleep)

    limiter.wait()
    limiter.wait()
    t[0] += 0.5
    limiter.wait()
    t[0] += 5
    limiter.wait()

    assert sleeps == pytest.approx([1.05, 0.55])


def test_rate_limiter_attached_to_client_covers_every_request():
    calls: list[int] = []
    limiter = RateLimiter(0)
    limiter.wait = lambda: calls.append(1)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))

    limiter.attach(client)
    client.get("https://example.test/a")
    client.get("https://example.test/b")

    assert len(calls) == 2


def test_floor_hour_uses_utc_hour_boundaries():
    moment = datetime(2026, 10, 20, 20, 59, 59, tzinfo=UTC)

    assert floor_hour(moment) == datetime(2026, 10, 20, 20, tzinfo=UTC)


def test_start_queues_rchg_spread_out_plans_and_fchg(conn, api, clock):
    collector = make_collector(conn, api, clock)

    assert collector.pending(Kind.RCHG) == 2
    assert collector.pending(Kind.PLAN) == 2 * PLAN_HOURS
    assert collector.pending(Kind.FCHG) == 2
    rchg_due = sorted(t.due for t in collector._queues[Kind.RCHG])
    assert rchg_due == [START, START + RCHG_INTERVAL / 2]


def test_tasks_run_in_priority_order(conn, api, clock):
    collector = make_collector(conn, api, clock, evas=[FRANKFURT])

    kinds = [t.kind for t in run_tasks(collector, clock, PLAN_HOURS + 2)]

    # rchg first, then every plan hour, then fchg once the backfill is done.
    assert kinds == [Kind.RCHG] + [Kind.PLAN] * PLAN_HOURS + [Kind.FCHG]


def test_plan_hours_upcoming_first_then_backwards(conn, api, clock):
    collector = make_collector(conn, api, clock, evas=[FRANKFURT])

    plans = [t for t in run_tasks(collector, clock, PLAN_HOURS + 1) if t.kind is Kind.PLAN]

    offsets = [int((t.hour - floor_hour(START)).total_seconds() // 3600) for t in plans]
    assert offsets == [0, 1, 2, 3, 4] + list(range(-1, -PLAN_HOURS_BACK - 1, -1))


def test_plan_requests_use_german_local_hour(conn, api, clock):
    collector = make_collector(conn, api, clock, evas=[FRANKFURT])

    run_tasks(collector, clock, 2)

    # 18:00 UTC is 20:00 in Germany.
    assert api.paths[:2] == [f"/rchg/{FRANKFURT}", f"/plan/{FRANKFURT}/261020/20"]


def rchg_run_times(collector: Collector, clock: FakeClock, count: int) -> list[datetime]:
    """Runs tasks until ``count`` rchg polls have run.

    Args:
        collector: The collector.
        clock: The fake clock.
        count: How many rchg polls to wait for.

    Returns:
        The clock time at which each rchg poll ran.
    """
    times = []
    while len(times) < count:
        now = clock.now()
        task = collector.run_next()
        if task is None:
            clock.sleep(collector.seconds_until_next_task(now) or 1)
        elif task.kind is Kind.RCHG:
            times.append(now)
    return times


def test_rchg_repeats_every_90_seconds(conn, api, clock):
    collector = make_collector(conn, api, clock, evas=[FRANKFURT])

    times = rchg_run_times(collector, clock, 4)

    assert {b - a for a, b in zip(times, times[1:])} == {RCHG_INTERVAL}


def test_rchg_catches_up_after_falling_behind(conn, api, clock):
    collector = make_collector(conn, api, clock, evas=[FRANKFURT])
    rchg_run_times(collector, clock, 1)
    late = START + timedelta(minutes=10)
    clock.current = late  # e.g. the machine was suspended

    times = rchg_run_times(collector, clock, 3)

    # One poll straight away, then the normal rhythm from there.
    assert times == [late, late + RCHG_INTERVAL, late + 2 * RCHG_INTERVAL]


def test_end_to_end_stores_plan_and_changes(conn, api, clock):
    collector = make_collector(conn, api, clock, evas=[FRANKFURT])

    run_tasks(collector, clock, PLAN_HOURS + 2)

    assert conn.execute("SELECT COUNT(*) FROM stops").fetchone()[0] == 10
    assert conn.execute("SELECT COUNT(*) FROM stop_changes").fetchone()[0] == 10
    assert collector.stats["fchg_saved"] == 10
    assert collector.stats["plan_saved"] == 7


def test_new_hour_is_added_to_the_plan_window(conn, api, clock):
    collector = make_collector(conn, api, clock, evas=[FRANKFURT])
    run_tasks(collector, clock, PLAN_HOURS + 2)
    assert collector.pending(Kind.PLAN) == 0

    clock.current = START + timedelta(hours=1, minutes=1)
    collector.run_next()

    hours = [t.hour for t in collector._queues[Kind.PLAN]]
    assert hours == [floor_hour(START) + timedelta(hours=PLAN_HOURS_AHEAD + 1)]


def test_failed_request_is_logged_and_the_loop_continues(conn, api, clock, caplog):
    api.fail[f"/rchg/{FRANKFURT}"] = 401
    collector = make_collector(conn, api, clock, evas=[FRANKFURT])

    first = collector.run_next()
    second = collector.run_next()

    assert first.kind is Kind.RCHG and second.kind is Kind.PLAN
    assert collector.stats["errors"] == 1
    assert "rchg for EVA 8000105 failed" in caplog.text
    # The poll keeps its rhythm.
    assert collector._queues[Kind.RCHG][0].due == START + RCHG_INTERVAL


def test_failed_plan_is_retried_until_it_succeeds(conn, api, clock):
    path = f"/plan/{FRANKFURT}/261020/20"
    api.fail[path] = 401
    collector = make_collector(conn, api, clock, evas=[FRANKFURT])
    run_tasks(collector, clock, 3)  # rchg, the failing plan, the next plan
    assert api.paths.count(path) == 1

    del api.fail[path]  # the API recovers
    clock.current = START + PLAN_RETRY_DELAY
    run_tasks(collector, clock, PLAN_HOURS + 2)

    assert api.paths.count(path) == 2
    assert conn.execute("SELECT COUNT(*) FROM stops").fetchone()[0] > 0


def test_failed_plan_is_given_up_once_out_of_the_window(conn, api, clock):
    path = f"/plan/{FRANKFURT}/261020/20"
    api.fail[path] = 401
    collector = make_collector(conn, api, clock, evas=[FRANKFURT])
    run_tasks(collector, clock, 3)

    clock.current = START + timedelta(hours=PLAN_HOURS_BACK + 1)
    run_tasks(collector, clock, 30)

    assert collector.stats["plan_given_up"] == 1
    assert all(t.hour != floor_hour(START) for t in collector._queues[Kind.PLAN])


def test_requests_retry_once_quickly(conn, api, clock, monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(api_module.time, "sleep", waits.append)
    api.fail[f"/rchg/{FRANKFURT}"] = 503
    collector = make_collector(conn, api, clock, evas=[FRANKFURT])

    collector.run_next()

    assert api.paths.count(f"/rchg/{FRANKFURT}") == 2
    assert waits == [1.0]
    assert collector.stats["errors"] == 1


def test_too_many_evas_for_the_rate_limit_is_refused(conn, api, clock):
    with pytest.raises(ValueError, match="86 EVA numbers"):
        make_collector(conn, api, clock, evas=list(range(86)))


def test_plan_window_backfills_hours_missed_during_a_pause(conn, api, clock):
    collector = make_collector(conn, api, clock, evas=[FRANKFURT])
    run_tasks(collector, clock, PLAN_HOURS + 2)

    clock.current = START + timedelta(hours=10)  # suspended for 10 hours
    collector.run_next()

    queued = sorted(t.hour for t in collector._queues[Kind.PLAN])
    # Everything from the old window's end up to 4 hours ahead of now.
    first_new = floor_hour(START) + timedelta(hours=PLAN_HOURS_AHEAD + 1)
    last_new = floor_hour(clock.current) + timedelta(hours=PLAN_HOURS_AHEAD)
    assert queued[0] == first_new and queued[-1] == last_new
    assert len(queued) == (last_new - first_new) // timedelta(hours=1) + 1


def test_plan_hour_repeated_by_the_clock_change_is_fetched_once(conn, api):
    # 00:00 and 01:00 UTC on 25 Oct are both 02:00 in Germany.
    clock = FakeClock(datetime(2026, 10, 25, 0, 30, tzinfo=UTC))
    collector = make_collector(conn, api, clock, evas=[FRANKFURT])

    run_tasks(collector, clock, PLAN_HOURS + 1)

    plan_paths = [p for p in api.paths if p.startswith("/plan/")]
    assert plan_paths.count(f"/plan/{FRANKFURT}/261025/02") == 1
    assert len(plan_paths) == len(set(plan_paths))


def test_local_hour_label():
    assert local_hour_label(datetime(2026, 10, 20, 18, tzinfo=UTC)) == "26102020"
    assert local_hour_label(datetime(2026, 10, 25, 0, tzinfo=UTC)) == "26102502"
    assert local_hour_label(datetime(2026, 10, 25, 1, tzinfo=UTC)) == "26102502"


def test_interruptible_sleep_returns_once_stopped():
    stop = threading.Event()
    sleep = interruptible_sleep(stop)

    stop.set()
    started = time.monotonic()
    sleep(30)

    assert time.monotonic() - started < 0.5


def test_interruptible_sleep_sleeps_when_not_stopped():
    started = time.monotonic()

    interruptible_sleep(threading.Event())(0.05)

    assert time.monotonic() - started >= 0.05


def test_run_stops_when_asked(conn, api, clock):
    stop = threading.Event()
    collector = make_collector(conn, api, clock, evas=[FRANKFURT], stop=stop)
    original = collector.run_next

    def run_next_then_stop():
        """Runs one task, then asks the collector to stop.

        Returns:
            The task that ran.
        """
        task = original()
        stop.set()
        return task

    collector.run_next = run_next_then_stop
    collector.run()

    assert len(api.paths) == 1


def test_summary_is_logged_every_five_minutes(conn, api, clock, caplog):
    caplog.set_level("INFO")
    collector = make_collector(conn, api, clock, evas=[FRANKFURT])
    run_tasks(collector, clock, 3)

    collector.log_summary_if_due()
    assert "Last" not in caplog.text

    clock.current = START + timedelta(minutes=5)
    collector.log_summary_if_due()
    assert "rchg 1 requests" in caplog.text
    assert collector.stats["rchg_requests"] == 0
