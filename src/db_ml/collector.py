"""Polls the Timetables API around the clock and stores what it sees.

Three kinds of task, in priority order:

1. ``/rchg`` for every EVA every 90 seconds. Entries cover the last two
   minutes, so live changes are never missed (D10, D13).
2. ``/plan`` for every hour from 12 hours ago to 4 hours ahead. Past hours
   matter because ``/fchg`` reaches back 8-12 hours and its changes need a
   stop to attach to; new hours are added as time moves on (D13).
3. ``/fchg`` for every EVA every hour, as a catch-up for anything ``/rchg``
   missed, e.g. while the collector was down.

A shared rate limiter keeps every request, retries included, under the
Timetables API's limit of 60 per minute. Requests retry once, briefly, so
one slow station can't hold up everyone else's ``/rchg``. A failed task is
logged and the loop carries on; a failed ``/plan`` is retried every few
minutes while its hour is still in the window (D15).

Example:
    Collect into the database at ``DB_ML_DATABASE_PATH``::

        $ uv run db-ml-collector
"""

from __future__ import annotations

import heapq
import itertools
import logging
import signal
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import IntEnum

import httpx

from db_ml.db import (
    SaveResult,
    collected_evas,
    connect,
    database_path,
    init_db,
    save_changes,
    save_plan,
)
from db_ml.timetable_xml import BERLIN, parse_timetable
from db_ml.timetables import (
    MIN_SECONDS_BETWEEN_REQUESTS,
    build_client,
    fetch_changes_xml,
    fetch_plan_xml,
    load_config,
)

logger = logging.getLogger(__name__)

RCHG_INTERVAL = timedelta(seconds=90)
FCHG_INTERVAL = timedelta(hours=1)
PLAN_HOURS_AHEAD = 4
PLAN_HOURS_BACK = 12
PLAN_RETRY_DELAY = timedelta(minutes=5)
SUMMARY_INTERVAL = timedelta(minutes=5)
# How long to remember which plan hours were fetched.
PLAN_MEMORY = timedelta(days=2)
# Upper bound on one idle sleep, so new plan hours are noticed promptly.
MAX_IDLE_SECONDS = 30.0
# /rchg may use at most this share of the request budget, so /plan and
# /fchg always get a turn.
MAX_RCHG_SHARE = 0.8
# /rchg has about 30 s of slack (2-minute window, 90 s interval), so one
# request must never take long: one quick retry, short waits, short timeout.
RETRY_OPTIONS = {"attempts": 2, "backoff_seconds": 1.0, "max_delay_seconds": 5.0}
REQUEST_TIMEOUT_SECONDS = 10.0


class Kind(IntEnum):
    """A kind of collector task. Lower values run first when both are due."""

    RCHG = 0
    PLAN = 1
    FCHG = 2


@dataclass(order=True)
class Task:
    """One request to make, and when.

    Attributes:
        due: When the task should run (aware, UTC).
        seq: Tie-breaker, so tasks due at the same time run in the order
            they were added.
        kind: What to fetch.
        eva: The EVA number to fetch for.
        hour: The hour to fetch, for ``/plan`` tasks (aware, UTC).
        attempts: How many times the task has failed so far.
    """

    due: datetime
    seq: int
    kind: Kind = field(compare=False)
    eva: int = field(compare=False)
    hour: datetime | None = field(default=None, compare=False)
    attempts: int = field(default=0, compare=False)


class RateLimiter:
    """Spaces calls at least a minimum interval apart.

    Attributes:
        min_interval: Seconds between the starts of two calls.
    """

    def __init__(
        self,
        min_interval: float = MIN_SECONDS_BETWEEN_REQUESTS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Creates a rate limiter.

        Args:
            min_interval: Seconds between the starts of two calls.
            monotonic: Clock to measure with; replaceable in tests.
            sleep: Called to wait; replaceable in tests.
        """
        self.min_interval = min_interval
        self._monotonic = monotonic
        self._sleep = sleep
        self._next_allowed: float | None = None

    def wait(self) -> None:
        """Blocks until the next call is allowed, then reserves that slot."""
        now = self._monotonic()
        if self._next_allowed is not None and now < self._next_allowed:
            self._sleep(self._next_allowed - now)
            now = self._next_allowed
        self._next_allowed = now + self.min_interval

    def attach(self, client: httpx.Client) -> None:
        """Makes every request the client sends, retries included, wait its turn.

        Args:
            client: The HTTP client to limit.
        """
        hooks = client.event_hooks
        hooks["request"] = [*hooks.get("request", []), lambda request: self.wait()]
        client.event_hooks = hooks


def floor_hour(moment: datetime) -> datetime:
    """Rounds a time down to the start of its hour, in UTC.

    German offsets are whole hours, so UTC hour boundaries are also German
    hour boundaries.

    Args:
        moment: An aware datetime.

    Returns:
        The start of the hour, in UTC.
    """
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def local_hour_label(hour: datetime) -> str:
    """Names an hour the way the ``/plan`` URL does.

    Args:
        hour: An aware datetime.

    Returns:
        The German local date and hour as ``YYMMDDHH``. On the night clocks
        go back, two UTC hours share the label ``...02``; the API can't tell
        them apart either, so that ``/plan`` is fetched once.
    """
    return hour.astimezone(BERLIN).strftime("%y%m%d%H")


def plan_offsets() -> list[int]:
    """Lists the plan hours to keep fetched, relative to the current hour.

    Returns:
        The current and upcoming hours first, then going back, so the hours
        that matter soonest are fetched first.
    """
    return list(range(PLAN_HOURS_AHEAD + 1)) + list(range(-1, -PLAN_HOURS_BACK - 1, -1))


def interruptible_sleep(stop: threading.Event) -> Callable[[float], None]:
    """Makes a sleep function that returns early once ``stop`` is set.

    It polls the event once a second instead of calling ``stop.wait``.
    A signal handler calls ``stop.set()``, which takes the event's internal
    lock; if the main thread were inside ``stop.wait`` holding that lock at
    that moment, the handler would deadlock.

    Args:
        stop: The event that signals shutdown.

    Returns:
        A function that sleeps for up to the given number of seconds.
    """

    def sleep(seconds: float) -> None:
        """Sleeps, waking every second to check for shutdown.

        Args:
            seconds: The longest time to sleep.
        """
        end = time.monotonic() + seconds
        while not stop.is_set():
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(left, 1.0))

    return sleep


class Collector:
    """Schedules and runs the collector's requests.

    Attributes:
        stats: Counts of what happened since the last summary.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        client: httpx.Client,
        evas: list[int],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        stop: threading.Event | None = None,
    ) -> None:
        """Creates a collector. Call ``start`` before ``run``.

        Args:
            conn: An open database connection.
            client: A Timetables HTTP client, ideally with a RateLimiter
                attached.
            evas: The EVA numbers to collect.
            now: Returns the current time (aware); replaceable in tests.
            sleep: Called to wait when nothing is due; replaceable in tests.
            stop: Set it to make ``run`` return after the current task.

        Raises:
            ValueError: If polling every EVA's ``/rchg`` would leave too
                little of the rate limit for ``/plan`` and ``/fchg``.
        """
        rchg_seconds = len(evas) * MIN_SECONDS_BETWEEN_REQUESTS
        if rchg_seconds > RCHG_INTERVAL.total_seconds() * MAX_RCHG_SHARE:
            raise ValueError(
                f"{len(evas)} EVA numbers need {rchg_seconds:.0f} s of requests per "
                f"{RCHG_INTERVAL.total_seconds():.0f} s /rchg round, leaving too little "
                "for /plan and /fchg. Collect fewer EVAs or lengthen RCHG_INTERVAL."
            )
        self._conn = conn
        self._client = client
        self._evas = list(evas)
        self._now = now
        self._sleep = sleep
        self._stop = stop or threading.Event()
        self._queues: dict[Kind, list[Task]] = {kind: [] for kind in Kind}
        self._seq = itertools.count()
        # (EVA, local hour label) -> the hour (UTC), for plans queued or done.
        self._planned: dict[tuple[int, str], datetime] = {}
        self._window_hour: datetime | None = None
        self._last_summary = now()
        self.stats: Counter[str] = Counter()

    def _add(self, kind: Kind, eva: int, due: datetime, hour: datetime | None = None, attempts: int = 0) -> None:
        """Queues a task.

        Args:
            kind: What to fetch.
            eva: The EVA number.
            due: When to run it.
            hour: The hour, for ``/plan`` tasks.
            attempts: Failures so far.
        """
        heapq.heappush(self._queues[kind], Task(due, next(self._seq), kind, eva, hour, attempts))

    def pending(self, kind: Kind) -> int:
        """Counts queued tasks of one kind.

        Args:
            kind: The kind of task.

        Returns:
            How many are queued.
        """
        return len(self._queues[kind])

    def start(self) -> None:
        """Queues the startup work.

        ``/rchg`` polls are spread evenly over the first 90 seconds so they
        don't all fall due at once. Then the plan window is filled. ``/fchg``
        is queued last; as the lowest priority it runs once the plans are
        fetched, so its changes have stops to attach to.
        """
        now = self._now()
        spacing = RCHG_INTERVAL / max(len(self._evas), 1)
        for i, eva in enumerate(self._evas):
            self._add(Kind.RCHG, eva, now + spacing * i)
        self._update_plan_window(now)
        for eva in self._evas:
            self._add(Kind.FCHG, eva, now)

    def _update_plan_window(self, now: datetime) -> None:
        """Queues plans for every hour in the window that isn't done yet.

        The window runs from 12 hours back to 4 hours ahead. It only moves
        when the hour changes, so most calls return straight away. After a
        long pause (e.g. the machine was suspended), the hours missed in
        between are queued too, as long as they're still in the window.
        Plan hours older than two days are forgotten, so memory stays flat.

        Args:
            now: The current time.
        """
        current = floor_hour(now)
        if current == self._window_hour:
            return
        self._window_hour = current
        for offset in plan_offsets():
            hour = current + timedelta(hours=offset)
            for eva in self._evas:
                key = (eva, local_hour_label(hour))
                if key not in self._planned:
                    self._planned[key] = hour
                    self._add(Kind.PLAN, eva, now, hour)
        cutoff = current - PLAN_MEMORY
        self._planned = {key: hour for key, hour in self._planned.items() if hour >= cutoff}

    def _next_due(self, now: datetime) -> Task | None:
        """Takes the highest-priority task that is due.

        Args:
            now: The current time.

        Returns:
            The task, removed from its queue, or None if nothing is due.
        """
        for kind in Kind:
            queue = self._queues[kind]
            if queue and queue[0].due <= now:
                return heapq.heappop(queue)
        return None

    def seconds_until_next_task(self, now: datetime) -> float:
        """Works out how long the loop can sleep.

        Args:
            now: The current time.

        Returns:
            Seconds until the earliest queued task, at most 30.
        """
        heads = [queue[0].due for queue in self._queues.values() if queue]
        if not heads:
            return MAX_IDLE_SECONDS
        return min(max((min(heads) - now).total_seconds(), 0.0), MAX_IDLE_SECONDS)

    def run_next(self) -> Task | None:
        """Runs the most urgent due task, if any.

        Returns:
            The task that ran, or None if nothing was due.
        """
        now = self._now()
        self._update_plan_window(now)
        task = self._next_due(now)
        if task is None:
            return None
        try:
            result = self._execute(task)
        except Exception:  # The collector must keep running; log and move on.
            logger.exception("%s for EVA %s failed", task.kind.name.lower(), task.eva)
            self.stats["errors"] += 1
            self._reschedule_failed(task)
        else:
            self._record(task, result)
            self._reschedule(task)
        return task

    def _execute(self, task: Task) -> SaveResult:
        """Fetches and stores one task's data.

        Args:
            task: The task to run.

        Returns:
            What was saved.
        """
        fetched_at = self._now()
        if task.kind is Kind.PLAN:
            content = fetch_plan_xml(self._client, task.eva, task.hour, **RETRY_OPTIONS)
            if content is None:
                return SaveResult()
            return save_plan(self._conn, parse_timetable(content, task.eva, fetched_at), fetched_at)
        recent = task.kind is Kind.RCHG
        content = fetch_changes_xml(self._client, task.eva, recent=recent, **RETRY_OPTIONS)
        if content is None:
            return SaveResult()
        stops = parse_timetable(content, task.eva, fetched_at)
        return save_changes(self._conn, stops, fetched_at, "rchg" if recent else "fchg")

    def _record(self, task: Task, result: SaveResult) -> None:
        """Adds a finished task to the stats.

        Args:
            task: The task that ran.
            result: What it saved.
        """
        name = task.kind.name.lower()
        self.stats[f"{name}_requests"] += 1
        self.stats[f"{name}_saved"] += result.saved
        self.stats[f"{name}_unchanged"] += result.unchanged
        self.stats[f"{name}_skipped"] += result.skipped

    def _reschedule(self, task: Task) -> None:
        """Queues the next run of a recurring task.

        The next run is counted from when this one was due, not when it
        finished, so the polling rhythm doesn't drift. If a whole interval
        was missed (e.g. the machine was suspended), this run was the
        catch-up, and the next one is a full interval after it.

        Args:
            task: The task that ran.
        """
        interval = {Kind.RCHG: RCHG_INTERVAL, Kind.FCHG: FCHG_INTERVAL}.get(task.kind)
        if interval is None:
            return
        now = self._now()
        due = task.due + interval
        self._add(task.kind, task.eva, due if due > now else now + interval)

    def _reschedule_failed(self, task: Task) -> None:
        """Queues a failed task again.

        Recurring tasks keep their rhythm. A failed ``/plan`` is retried
        every five minutes for as long as its hour is in the window, so an
        outage of the API doesn't lose the hour for good.

        Args:
            task: The task that failed.
        """
        if task.kind is not Kind.PLAN:
            self._reschedule(task)
            return
        now = self._now()
        if task.hour >= floor_hour(now) - timedelta(hours=PLAN_HOURS_BACK):
            self._add(Kind.PLAN, task.eva, now + PLAN_RETRY_DELAY, task.hour, task.attempts + 1)
        else:
            logger.warning(
                "Giving up on plan for EVA %s, hour %s, after %d attempts: it's out of the window",
                task.eva, task.hour, task.attempts + 1,
            )
            self.stats["plan_given_up"] += 1

    def log_summary_if_due(self) -> None:
        """Logs what happened since the last summary, every five minutes."""
        now = self._now()
        if now - self._last_summary < SUMMARY_INTERVAL:
            return
        s = self.stats
        logger.info(
            "Last %d min: rchg %d requests, %d changed; fchg %d requests, %d changed; "
            "plan %d requests, %d stops; %d errors; %d plan fetches queued",
            (now - self._last_summary).total_seconds() // 60,
            s["rchg_requests"], s["rchg_saved"], s["fchg_requests"], s["fchg_saved"],
            s["plan_requests"], s["plan_saved"], s["errors"], self.pending(Kind.PLAN),
        )
        self.stats.clear()
        self._last_summary = now

    def run(self) -> None:
        """Runs tasks until ``stop`` is set."""
        while not self._stop.is_set():
            if self.run_next() is None:
                self._sleep(self.seconds_until_next_task(self._now()))
            self.log_summary_if_due()


def main() -> None:
    """Runs the collector until it receives SIGINT or SIGTERM."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request at INFO, which would bury the summaries.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    conn = connect(database_path())
    init_db(conn)
    evas = collected_evas(conn)
    if not evas:
        raise SystemExit("No EVA numbers to collect. Run `uv run python -m db_ml.db` first.")

    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())

    with build_client(load_config()) as client:
        client.timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS)
        RateLimiter().attach(client)
        collector = Collector(conn, client, evas, sleep=interruptible_sleep(stop), stop=stop)
        collector.start()
        logger.info("Collecting %d EVA numbers into %s", len(evas), database_path())
        collector.run()
    conn.close()
    logger.info("Stopped")


if __name__ == "__main__":
    main()
