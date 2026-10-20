"""Tests for the Wikidata city population lookup."""

from urllib.parse import parse_qs

import httpx

from db_ml.city_populations import (
    Municipality,
    build_query_by_code,
    build_query_by_name,
    fetch_populations,
    group_by_municipality,
    in_same_state,
    parse_results,
)


def binding(
    key: str,
    qid: str,
    label: str,
    population: int,
    date: str | None = None,
    code: str | None = None,
):
    """Builds one fake SPARQL result row.

    Args:
        key: The lookup key (municipality code or city name).
        qid: The Wikidata item ID.
        label: The item's label.
        population: The population value.
        date: Optional point-in-time for the figure.
        code: Optional municipality key of the item (name lookups only).

    Returns:
        A dict shaped like a Wikidata SPARQL JSON binding.
    """
    row = {
        "key": {"value": key},
        "item": {"value": f"http://www.wikidata.org/entity/{qid}"},
        "itemLabel": {"value": label},
        "population": {"value": str(population)},
    }
    if date:
        row["date"] = {"value": date}
    if code:
        row["code"] = {"value": code}
    return row


def sparql_response(rows: list[dict]) -> httpx.Response:
    """Wraps result rows in a Wikidata-style JSON response.

    Args:
        rows: SPARQL bindings to return.

    Returns:
        An httpx.Response with status 200.
    """
    return httpx.Response(200, json={"results": {"bindings": rows}})


def test_group_by_municipality_merges_shared_codes_and_strips_names():
    stations = [
        {"name": "Mönchengladbach Hbf", "municipalityCode": "05116000",
         "mailingAddress": {"city": "Mönchengladbach"}},
        {"name": "Rheydt Hbf", "municipalityCode": "05116000",
         "mailingAddress": {"city": "Mönchengladbach"}},
        {"name": "Bingen Hbf", "municipalityCode": "07339005",
         "mailingAddress": {"city": "Bingen "}},
        {"name": "No Code Hbf"},
    ]

    result = group_by_municipality(stations)

    assert result == [
        Municipality("05116000", "Mönchengladbach",
                     ["Mönchengladbach Hbf", "Rheydt Hbf"]),
        Municipality("07339005", "Bingen", ["Bingen Hbf"]),
    ]


def test_group_by_municipality_handles_null_address_and_city():
    stations = [
        {"name": "A Hbf", "municipalityCode": "1", "mailingAddress": None},
        {"name": "B Hbf", "municipalityCode": "2", "mailingAddress": {"city": None}},
        {"name": "C Hbf", "municipalityCode": "3"},
    ]

    assert [m.city for m in group_by_municipality(stations)] == ["", "", ""]


def test_queries_include_values_and_escape_quotes():
    assert '"05334002" "09162000"' in build_query_by_code(["05334002", "09162000"])
    assert 'wdt:P439 ?key' in build_query_by_code(["05334002"])
    assert '"Say \\"Hi\\""' in build_query_by_name(['Say "Hi"'])
    assert "?code" in build_query_by_name(["Hanau"])


def test_parse_results_uses_latest_figure_per_item():
    rows = [
        binding("14522080", "Q71163", "Döbeln", 23728, "2023-12-31T00:00:00Z"),
        binding("14522080", "Q71163", "Döbeln", 23907, "2025-12-31T00:00:00Z"),
    ]

    assert parse_results(rows)["14522080"].population == 23907


def test_parse_results_prefers_dated_figure_over_undated():
    rows = [
        binding("k", "Q1", "Town", 500),
        binding("k", "Q1", "Town", 400, "2024-12-31T00:00:00Z"),
    ]

    assert parse_results(rows)["k"].population == 400


def test_parse_results_picks_most_populous_item_for_ambiguous_key():
    rows = [
        binding("Neustadt", "Q1", "Neustadt", 5_000),
        binding("Neustadt", "Q2", "Neustadt", 50_000),
    ]

    record = parse_results(rows)["Neustadt"]

    assert record.wikidata_id == "Q2"
    assert record.population == 50_000


def test_in_same_state_keeps_only_matches_in_the_stations_state():
    # Two towns called Neustadt: one in Rheinland-Pfalz (07), one in Hessen (06).
    rows = [
        binding("Neustadt", "Q1", "Neustadt", 50_000, code="07316000"),
        binding("Neustadt", "Q2", "Neustadt", 9_000, code="06534015"),
        binding("Hanau", "Q3802", "Hanau", 98_582, code="06415000"),
    ]
    hessian_neustadt = Municipality("06534999", "Neustadt")

    result = in_same_state(rows, hessian_neustadt)

    assert [r["item"]["value"].rsplit("/", 1)[-1] for r in result] == ["Q2"]


def test_fetch_populations_falls_back_to_name_within_state_and_sorts():
    municipalities = [
        Municipality("05334002", "Aachen", ["Aachen Hbf"]),
        # Hanau's old key; Wikidata only knows the new one (06415000).
        Municipality("06435014", "Hanau", ["Hanau Hbf"]),
        # Only a same-named town in another state exists: must not match.
        Municipality("09999999", "Nowhere", ["Nowhere Hbf"]),
    ]
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.content.decode())["query"][0]
        queries.append(query)
        assert request.headers["User-Agent"].startswith("db-ml")
        if "wdt:P439 ?key" in query:
            return sparql_response([binding("05334002", "Q1017", "Aachen", 261_000)])
        return sparql_response([
            binding("Hanau", "Q3802", "Hanau", 98_582, code="06415000"),
            binding("Nowhere", "Q9", "Nowhere", 500_000, code="05111000"),
        ])

    client = httpx.Client(transport=httpx.MockTransport(handler))

    result = fetch_populations(client, municipalities)

    assert [r["city"] for r in result] == ["Aachen", "Hanau", "Nowhere"]
    assert [r["matchedBy"] for r in result] == ["municipalityCode", "name", None]
    assert result[1]["population"] == 98_582
    assert result[2]["population"] is None
    assert len(queries) == 2
    assert '"Hanau"' in queries[1] and '"Nowhere"' in queries[1]


def test_fetch_populations_batches_queries():
    municipalities = [Municipality(f"{i:08d}", f"City {i}") for i in range(5)]
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.content.decode())["query"][0]
        queries.append(query)
        codes = [m.code for m in municipalities if f'"{m.code}"' in query]
        return sparql_response([binding(c, "Q1", "City", 1) for c in codes])

    client = httpx.Client(transport=httpx.MockTransport(handler))

    fetch_populations(client, municipalities, batch_size=2)

    # 5 codes in batches of 2, all matched, so no name lookups.
    assert len(queries) == 3
