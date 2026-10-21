"""Turns Timetables API XML into plain records.

``/plan``, ``/fchg`` and ``/rchg`` all return a ``<timetable>`` of ``<s>``
stops with the same structure. ``/plan`` fills the planned attributes
(``pt``, ``pp``, ``ppth``). The change feeds fill the changed ones (``ct``,
``cp``, ``cs``, ``clt``, ``cpth``) and messages, and each ``/rchg`` entry carries the stop's
full current change state, exactly as ``/fchg`` does (checked 2026-10-20).

Times arrive as ``YYMMDDHHmm`` in German local time with no UTC offset. On
the night clocks go back (e.g. 25 Oct 2026), 02:00-02:59 happens twice, so
such times are resolved using what we know about the stop; see
``parse_db_time``. One limitation remains: a planned time in that repeated
hour, at a stop with no earlier time to anchor it, is resolved against the
fetch time, which can pick the wrong occurrence if the plan was fetched
hours in advance.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

BERLIN = ZoneInfo("Europe/Berlin")
S_BAHN_CATEGORY = "S"
# Trains can leave a little early but practically never this much.
MAX_EARLY = timedelta(minutes=10)


@dataclass(frozen=True)
class Event:
    """An arrival or departure at a stop.

    Attributes:
        planned_time: When it's scheduled (``pt``).
        planned_platform: The scheduled platform (``pp``).
        changed_time: The current forecast (``ct``), if DB has one.
        changed_platform: The new platform (``cp``), if it changed.
        cancelled: Whether it's currently cancelled (``cs="c"``). A
            reinstated event has ``cs="p"`` and counts as not cancelled.
        cancelled_at: When DB announced the cancellation (``clt``). It can
            stay set after the event is reinstated.
        planned_path: Stations before (arrival) or after (departure) this
            stop, separated by ``|`` (``ppth``).
        changed_path: The path after a change such as a diversion
            (``cpth``). Empty text means DB sent the attribute empty, which
            it does for cancelled events; None means it wasn't sent.
        line: The line name, e.g. ``"RE60"`` (``l``).
        wings: Trips this one is coupled to, as ``<trip id>-<trip start>``
            separated by ``|`` (``wings``).
    """

    planned_time: datetime | None = None
    planned_platform: str | None = None
    changed_time: datetime | None = None
    changed_platform: str | None = None
    cancelled: bool = False
    cancelled_at: datetime | None = None
    planned_path: str | None = None
    changed_path: str | None = None
    line: str | None = None
    wings: str | None = None


@dataclass(frozen=True)
class Message:
    """A message DB attached to a stop.

    Attributes:
        message_id: DB's message ID. The same message can appear at every
            stop of a trip.
        type: ``d`` delay reason, ``q`` quality notice, ``f`` free text,
            ``h`` disruption notice (e.g. construction work).
        code: The numeric reason code (``c``), if any.
        category: The disruption category (``cat``), e.g. ``"BAUARBEITEN"``.
        time: When DB issued it (``ts``).
        valid_from: Start of the period it applies to (``from``). Only
            disruption notices have one; for construction work it's known
            in advance.
        valid_to: End of that period (``to``).
        priority: DB's priority (``pr``), 1 being the highest.
    """

    message_id: str
    type: str | None
    code: int | None
    category: str | None
    time: datetime | None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    priority: int | None = None


@dataclass(frozen=True)
class Stop:
    """One train at one station, as described by one ``<s>`` element.

    Attributes:
        stop_id: The full ID, ``<trip id>-<trip start>-<stop index>``.
        trip_id: The trip ID, the same at every stop of the trip.
        trip_start: When the train left its first station.
        stop_index: The stop's position on the route, starting at 1.
        eva: The station's EVA number.
        category: Train category, e.g. ``"ICE"`` (``tl@c``). Change feeds
            usually leave this out.
        train_number: e.g. ``"372"`` (``tl@n``).
        operator: The operator's code (``tl@o``), e.g. ``"80"`` for DB
            Fernverkehr.
        arrival: The arrival, or None if the train starts here (or the feed
            has nothing about it).
        departure: The departure, or None if the train ends here (or the
            feed has nothing about it).
        messages: Messages on the stop and on its arrival and departure,
            each message ID once.
    """

    stop_id: str
    trip_id: str
    trip_start: datetime
    stop_index: int
    eva: int
    category: str | None = None
    train_number: str | None = None
    operator: str | None = None
    arrival: Event | None = None
    departure: Event | None = None
    messages: tuple[Message, ...] = field(default_factory=tuple)

    @property
    def has_train_details(self) -> bool:
        """Whether the record says which train this is.

        Returns:
            True if the category is known. ``/plan`` always includes it;
            change feeds only do for some trains, e.g. ones added at short
            notice.
        """
        return self.category is not None

    @property
    def is_s_bahn(self) -> bool:
        """Whether this is an S-Bahn train, which we don't collect (D8).

        Returns:
            True if the category is ``"S"``.
        """
        return self.category == S_BAHN_CATEGORY


def _local_candidates(naive: datetime) -> list[datetime]:
    """Lists the real moments a German wall-clock time can refer to.

    Args:
        naive: A wall-clock time without a time zone.

    Returns:
        One aware datetime normally; two, an hour apart, when the time
        happens twice (clocks going back). A time that never happened
        (02:00-02:59 when clocks go forward) is moved forward an hour,
        e.g. 02:30 becomes 03:30.
    """
    earlier = naive.replace(tzinfo=BERLIN, fold=0)
    later = naive.replace(tzinfo=BERLIN, fold=1)
    if earlier.utcoffset() == later.utcoffset():
        return [earlier]
    normalised = earlier.astimezone(UTC).astimezone(BERLIN)
    if normalised.replace(tzinfo=None) != naive:
        return [normalised]
    return [earlier, later]


def parse_db_time(
    text: str | None,
    reference: datetime,
    *,
    earliest: datetime | None = None,
    latest: datetime | None = None,
) -> datetime | None:
    """Converts a ``YYMMDDHHmm`` German local time into an aware datetime.

    When clocks go back, a local time between 02:00 and 02:59 happens
    twice, an hour apart, and the API doesn't say which is meant. Candidates
    outside ``earliest``/``latest`` are ruled out first (if that leaves any),
    then the one closest to ``reference`` is used.

    Args:
        text: The time as the API sends it, e.g. ``"2610201718"``, or None.
        reference: An aware datetime close to the expected answer.
        earliest: Optional lower bound the answer should respect.
        latest: Optional upper bound the answer should respect.

    Returns:
        The time in Europe/Berlin with the correct UTC offset, or None if
        ``text`` is empty.

    Raises:
        ValueError: If ``text`` isn't a valid ``YYMMDDHHmm`` time.
    """
    if not text:
        return None
    candidates = _local_candidates(datetime.strptime(text, "%y%m%d%H%M"))
    if len(candidates) == 1:
        return candidates[0]
    # Compare in UTC: subtracting two Berlin times ignores the offset.
    in_bounds = [
        c
        for c in candidates
        if (earliest is None or c.astimezone(UTC) >= earliest.astimezone(UTC))
        and (latest is None or c.astimezone(UTC) <= latest.astimezone(UTC))
    ]
    target = reference.astimezone(UTC)
    return min(in_bounds or candidates, key=lambda c: abs(c.astimezone(UTC) - target))


def parse_trip_start(text: str) -> datetime:
    """Converts the trip-start part of a stop ID into an aware datetime.

    Journeys are grouped on (trip ID, trip start), so every station must
    get the same answer for the same trip. When the time happens twice
    (clocks going back), the first occurrence is always used. A trip that
    really started in the second 02:00-02:59 hour is then an hour early,
    but consistently so at every station.

    Args:
        text: The trip start as ``YYMMDDHHmm``, e.g. ``"2610200804"``.

    Returns:
        The trip start in Europe/Berlin.

    Raises:
        ValueError: If ``text`` isn't a valid ``YYMMDDHHmm`` time.
    """
    return _local_candidates(datetime.strptime(text, "%y%m%d%H%M"))[0]


def split_stop_id(stop_id: str) -> tuple[str, str, int]:
    """Splits a stop ID into its three parts.

    Trip IDs can be negative, so the ID is split from the right.

    Args:
        stop_id: e.g. ``"-2041012609203177085-2610200804-18"``.

    Returns:
        The trip ID, the trip start as ``YYMMDDHHmm`` text, and the stop
        index, e.g. ``("-2041012609203177085", "2610200804", 18)``.

    Raises:
        ValueError: If the ID doesn't have three parts.
    """
    trip_id, trip_start, stop_index = stop_id.rsplit("-", 2)
    return trip_id, trip_start, int(stop_index)


def _event(
    element: ET.Element | None,
    reference: datetime,
    fetched_at: datetime,
    earliest: datetime | None = None,
) -> Event | None:
    """Reads an ``<ar>`` or ``<dp>`` element.

    Args:
        element: The element, or None if the stop doesn't have one.
        reference: Reference time for resolving the planned time.
        fetched_at: When the response was fetched. Cancellations were
            announced before then.
        earliest: Optional lower bound for the planned time (a departure
            can't be planned before its arrival).

    Returns:
        The Event, or None if there's no element.
    """
    if element is None:
        return None
    planned = parse_db_time(element.get("pt"), reference, earliest=earliest)
    # A forecast is close to the planned time and practically never much earlier.
    changed = parse_db_time(
        element.get("ct"),
        planned or reference,
        earliest=planned - MAX_EARLY if planned else None,
    )
    return Event(
        planned_time=planned,
        planned_platform=element.get("pp"),
        changed_time=changed,
        changed_platform=element.get("cp"),
        cancelled=element.get("cs") == "c",
        cancelled_at=parse_db_time(element.get("clt"), fetched_at, latest=fetched_at),
        planned_path=element.get("ppth"),
        changed_path=element.get("cpth"),
        line=element.get("l") or None,
        wings=element.get("wings"),
    )


def _int_or_none(value: str | None) -> int | None:
    """Converts a numeric attribute to an int.

    Args:
        value: The attribute value, or None.

    Returns:
        The integer, or None if the value is missing or not a number.
    """
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _messages(stop: ET.Element, fetched_at: datetime) -> tuple[Message, ...]:
    """Collects a stop's messages, each message ID once.

    Args:
        stop: The ``<s>`` element.
        fetched_at: When the response was fetched. Messages were issued
            before then.

    Returns:
        Messages on the stop itself first, then on its arrival and departure.
    """
    found: dict[str, Message] = {}
    elements = stop.findall("m") + stop.findall("ar/m") + stop.findall("dp/m")
    for m in elements:
        message_id = m.get("id")
        if not message_id or message_id in found:
            continue
        found[message_id] = Message(
            message_id=message_id,
            type=m.get("t"),
            code=_int_or_none(m.get("c")),
            category=m.get("cat"),
            time=parse_db_time(m.get("ts"), fetched_at, latest=fetched_at),
            valid_from=parse_db_time(m.get("from"), fetched_at),
            valid_to=parse_db_time(m.get("to"), fetched_at),
            priority=_int_or_none(m.get("pr")),
        )
    return tuple(found.values())


def _parse_stop(s: ET.Element, default_eva: int, fetched_at: datetime) -> Stop:
    """Reads one ``<s>`` element.

    Args:
        s: The element.
        default_eva: The EVA number to use if the element doesn't name one.
        fetched_at: When the response was fetched.

    Returns:
        The parsed Stop.

    Raises:
        ValueError, AttributeError, TypeError: If the element is malformed.
    """
    trip_id, trip_start_text, stop_index = split_stop_id(s.get("id"))
    arrival = _event(s.find("ar"), fetched_at, fetched_at)
    departure = _event(
        s.find("dp"),
        # A departure follows its arrival within minutes.
        arrival.planned_time if arrival and arrival.planned_time else fetched_at,
        fetched_at,
        earliest=arrival.planned_time if arrival else None,
    )
    train = s.find("tl")
    return Stop(
        stop_id=s.get("id"),
        trip_id=trip_id,
        trip_start=parse_trip_start(trip_start_text),
        stop_index=stop_index,
        eva=int(s.get("eva") or default_eva),
        category=train.get("c") if train is not None else None,
        train_number=train.get("n") if train is not None else None,
        operator=train.get("o") if train is not None else None,
        arrival=arrival,
        departure=departure,
        messages=_messages(s, fetched_at),
    )


def parse_timetable(content: bytes | str, eva: int, fetched_at: datetime) -> list[Stop]:
    """Parses a ``/plan``, ``/fchg`` or ``/rchg`` response.

    A malformed ``<s>`` element is logged and skipped, so one bad stop
    doesn't lose the rest of the response.

    Args:
        content: The response body.
        eva: The EVA number the request was for. Used when the XML doesn't
            name one (``/plan`` doesn't).
        fetched_at: When the response was fetched, as an aware datetime.
            Used to resolve ambiguous local times.

    Returns:
        One Stop per well-formed ``<s>`` element, in document order.

    Raises:
        xml.etree.ElementTree.ParseError: If the response isn't valid XML.
    """
    root = ET.fromstring(content)
    default_eva = int(root.get("eva") or eva)
    stops = []
    for s in root.findall("s"):
        try:
            stops.append(_parse_stop(s, default_eva, fetched_at))
        except (ValueError, AttributeError, TypeError) as error:
            logger.warning("Skipping malformed stop %r at EVA %s: %s", s.get("id"), eva, error)
    return stops
