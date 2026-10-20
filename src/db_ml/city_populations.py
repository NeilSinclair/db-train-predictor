"""Looks up the population of each city that has a main station.

Reads the cleaned stations file, takes each station's municipality, and
queries Wikidata for that municipality's population. Matching is done on
the official German municipality key (Amtlicher Gemeindeschlüssel, which
StaDa calls ``municipalityCode``), with a fallback to the city name for keys
that have changed (e.g. when a town becomes a district-free city).

Example:
    Write populations to ``data/raw/city_populations.json``::

        $ uv run python -m db_ml.city_populations
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from db_ml.api import request_with_retries
from db_ml.files import load_json, save_json

WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
# Wikidata blocks requests without a descriptive User-Agent.
USER_AGENT = "db-ml/0.1 (Deutsche Bahn delay prediction project)"
DEFAULT_INPUT_PATH = Path("data/raw/stations_clean.json")
DEFAULT_OUTPUT_PATH = Path("data/raw/city_populations.json")


@dataclass
class Municipality:
    """A municipality that has one or more main stations.

    Attributes:
        code: The 8-digit German municipality key, e.g. ``"05334002"``.
            The first two digits identify the federal state.
        city: The city name from the station's mailing address.
        stations: Names of the stations in this municipality.
    """

    code: str
    city: str
    stations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PopulationRecord:
    """A population figure for one Wikidata item.

    Attributes:
        wikidata_id: The Wikidata item ID, e.g. ``"Q1017"``.
        label: The item's German label.
        population: The number of inhabitants.
        date: The date the figure refers to (ISO 8601), if known.
    """

    wikidata_id: str
    label: str
    population: int
    date: str | None


def group_by_municipality(stations: list[dict[str, Any]]) -> list[Municipality]:
    """Groups stations by municipality code.

    Several stations can share a municipality (e.g. Mönchengladbach Hbf and
    Rheydt Hbf), so each municipality is looked up only once.

    Args:
        stations: StaDa station records.

    Returns:
        One Municipality per distinct code, in order of first appearance.
        Stations without a municipality code are skipped.
    """
    by_code: dict[str, Municipality] = {}
    for station in stations:
        code = station.get("municipalityCode")
        if not code:
            continue
        if code not in by_code:
            # The API may send null instead of leaving a field out.
            city = (station.get("mailingAddress") or {}).get("city") or ""
            by_code[code] = Municipality(code=code, city=city.strip())
        by_code[code].stations.append(station["name"])
    return list(by_code.values())


def _sparql_string(value: str) -> str:
    """Quotes a value as a SPARQL string literal.

    Args:
        value: The raw string.

    Returns:
        The string wrapped in double quotes with special characters escaped.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_query_by_code(codes: list[str]) -> str:
    """Builds a SPARQL query for populations by municipality key.

    Args:
        codes: German municipality keys to look up.

    Returns:
        A SPARQL query whose results have ``key``, ``item``, ``itemLabel``,
        ``population`` and optionally ``date`` bindings.
    """
    values = " ".join(_sparql_string(code) for code in codes)
    return f"""
SELECT ?key ?item ?itemLabel ?population ?date WHERE {{
  VALUES ?key {{ {values} }}
  ?item wdt:P439 ?key;
        p:P1082 ?statement.
  ?statement ps:P1082 ?population;
             a wikibase:BestRank.
  OPTIONAL {{ ?statement pq:P585 ?date. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "de". }}
}}"""


def build_query_by_name(names: list[str]) -> str:
    """Builds a SPARQL query for populations by German city name.

    Only items that have a German municipality key are considered, so the
    name won't match a river or a person.

    Args:
        names: German city names to look up.

    Returns:
        A SPARQL query with the same bindings as ``build_query_by_code``,
        where ``key`` is the city name, plus ``code``: the item's own
        municipality key.
    """
    values = " ".join(_sparql_string(name) for name in names)
    return f"""
SELECT ?key ?code ?item ?itemLabel ?population ?date WHERE {{
  VALUES ?key {{ {values} }}
  BIND(STRLANG(?key, "de") AS ?label)
  ?item rdfs:label ?label;
        wdt:P439 ?code;
        p:P1082 ?statement.
  ?statement ps:P1082 ?population;
             a wikibase:BestRank.
  OPTIONAL {{ ?statement pq:P585 ?date. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "de". }}
}}"""


def parse_results(bindings: list[dict[str, Any]]) -> dict[str, PopulationRecord]:
    """Picks one population figure per lookup key from SPARQL results.

    An item can have several population figures, so the most recent one is
    used. If a key matches several items (e.g. two towns with the same
    name), the most populous item wins.

    Args:
        bindings: The ``results.bindings`` list from a Wikidata response.

    Returns:
        A mapping from lookup key to its chosen PopulationRecord.
    """
    latest: dict[tuple[str, str], PopulationRecord] = {}
    for row in bindings:
        key = row["key"]["value"]
        record = PopulationRecord(
            wikidata_id=row["item"]["value"].rsplit("/", 1)[-1],
            label=row["itemLabel"]["value"],
            population=int(float(row["population"]["value"])),
            date=row.get("date", {}).get("value"),
        )
        current = latest.get((key, record.wikidata_id))
        if current is None or (record.date or "") > (current.date or ""):
            latest[(key, record.wikidata_id)] = record

    best: dict[str, PopulationRecord] = {}
    for (key, _), record in latest.items():
        if key not in best or record.population > best[key].population:
            best[key] = record
    return best


def in_same_state(bindings: list[dict[str, Any]], municipality: Municipality) -> list[dict[str, Any]]:
    """Keeps name-lookup results that could be the given municipality.

    A name match is only trusted within the same federal state, which the
    first two digits of the municipality key identify. This stops a small
    town from taking the population of a big city with the same name.

    Args:
        bindings: Rows from a ``build_query_by_name`` query.
        municipality: The municipality being looked up.

    Returns:
        The rows for the municipality's city name whose municipality key is
        in the same state.
    """
    state = municipality.code[:2]
    return [
        row
        for row in bindings
        if row["key"]["value"] == municipality.city and row["code"]["value"][:2] == state
    ]


def run_query(client: httpx.Client, query: str) -> list[dict[str, Any]]:
    """Sends a SPARQL query to Wikidata.

    Args:
        client: An HTTP client.
        query: The SPARQL query text.

    Returns:
        The ``results.bindings`` list from the response.

    Raises:
        httpx.HTTPStatusError: If Wikidata returns an error status, once
            retries are used up.
    """
    response = request_with_retries(
        client,
        "POST",
        WIKIDATA_SPARQL_URL,
        data={"query": query},
        headers={
            "Accept": "application/sparql-results+json",
            "User-Agent": USER_AGENT,
        },
        timeout=60.0,
    )
    response.raise_for_status()
    return response.json()["results"]["bindings"]


def _batches(items: list[str], size: int) -> list[list[str]]:
    """Splits a list into consecutive batches.

    Args:
        items: The items to split.
        size: The maximum batch size.

    Returns:
        A list of batches, each at most ``size`` long.
    """
    return [items[i : i + size] for i in range(0, len(items), size)]


def fetch_populations(
    client: httpx.Client, municipalities: list[Municipality], batch_size: int = 50
) -> list[dict[str, Any]]:
    """Looks up the population of each municipality on Wikidata.

    Municipalities are matched by code first; any that don't match are
    retried by city name, within the same federal state.

    Args:
        client: An HTTP client.
        municipalities: The municipalities to look up.
        batch_size: How many municipalities to put in one query.

    Returns:
        One dict per municipality, sorted by population (largest first).
        Municipalities with no match have ``population`` set to None and
        are placed last.
    """
    by_code: dict[str, PopulationRecord] = {}
    for batch in _batches([m.code for m in municipalities], batch_size):
        by_code.update(parse_results(run_query(client, build_query_by_code(batch))))

    unmatched = [m for m in municipalities if m.code not in by_code]
    names = sorted({m.city for m in unmatched if m.city})
    name_rows: list[dict[str, Any]] = []
    for batch in _batches(names, batch_size):
        name_rows += run_query(client, build_query_by_name(batch))

    results = []
    for municipality in municipalities:
        record = by_code.get(municipality.code)
        matched_by = "municipalityCode" if record else None
        if record is None:
            record = parse_results(in_same_state(name_rows, municipality)).get(municipality.city)
            matched_by = "name" if record else None
        results.append(
            {
                "municipalityCode": municipality.code,
                "city": municipality.city,
                "stations": municipality.stations,
                "population": record.population if record else None,
                "populationDate": record.date if record else None,
                "wikidataId": record.wikidata_id if record else None,
                "wikidataLabel": record.label if record else None,
                "matchedBy": matched_by,
            }
        )
    results.sort(key=lambda r: (r["population"] is None, -(r["population"] or 0)))
    return results


def main() -> None:
    """Looks up populations for all main-station cities and saves them."""
    municipalities = group_by_municipality(load_json(DEFAULT_INPUT_PATH))
    with httpx.Client() as client:
        populations = fetch_populations(client, municipalities)
    save_json(populations, DEFAULT_OUTPUT_PATH)

    unmatched = [p["city"] for p in populations if p["population"] is None]
    print(f"Saved {len(populations)} cities to {DEFAULT_OUTPUT_PATH}")
    if unmatched:
        print(f"No population found for: {', '.join(unmatched)}")


if __name__ == "__main__":
    main()
