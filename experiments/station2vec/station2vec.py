"""Trains word2vec on train routes, treating each station as a word.

A side experiment: every trip's route (the stations it calls at, in order)
is a sentence, so stations that share trains end up close together. The
vectors are projected to 2D with t-SNE and with PCA and written to JSON for
plotting.

Run it against a snapshot of the database, never the live file:

    uv run --group station2vec python experiments/station2vec/station2vec.py \
        data/db_ml_snapshot.sqlite data/station2vec.json
"""

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

LONG_DISTANCE = {"ICE", "IC", "EC", "ECE", "EN", "NJ", "EUR", "TGV", "RJ", "RJX", "FLX", "WB", "ES"}


def train_kind(category: str | None) -> str:
    """Groups a train category into a broad kind.

    Args:
        category: The Timetables category, e.g. "ICE", "RE" or "Bus".

    Returns:
        "long-distance", "bus" or "regional".
    """
    if category in LONG_DISTANCE:
        return "long-distance"
    if category == "Bus":
        return "bus"
    return "regional"


def trip_routes(conn: sqlite3.Connection) -> list[tuple[list[str], str]]:
    """Builds the full route of every trip in the database.

    A stop knows the stations before and after it, so its route is
    ``route_before + [this station] + route_after``. A trip is seen at
    several collected stations; the longest route seen for it is kept.

    Args:
        conn: An open connection to the collector database.

    Returns:
        One ``(stations, kind)`` pair per trip, where ``kind`` comes from
        ``train_kind``.
    """
    rows = conn.execute(
        """
        SELECT s.trip_id, s.trip_start, s.category, s.route_before, s.route_after,
               e.timetable_name
        FROM stops s JOIN station_evas e USING (eva)
        WHERE e.timetable_name IS NOT NULL
        """
    )
    longest: dict[tuple[str, str], tuple[list[str], str]] = {}
    for trip_id, trip_start, category, before, after, here in rows:
        route = [*filter(None, (before or "").split("|")), here, *filter(None, (after or "").split("|"))]
        key = (trip_id, trip_start)
        if key not in longest or len(route) > len(longest[key][0]):
            longest[key] = (route, train_kind(category))
    return list(longest.values())


def station_kinds(routes: list[tuple[list[str], str]]) -> dict[str, Counter]:
    """Counts the kinds of trip that call at each station.

    Args:
        routes: Routes from ``trip_routes``.

    Returns:
        For each station, a Counter of trip kinds.
    """
    kinds: dict[str, Counter] = defaultdict(Counter)
    for stations, kind in routes:
        for station in set(stations):
            kinds[station][kind] += 1
    return kinds


def train_and_project(sentences: list[list[str]], seed: int = 42) -> tuple:
    """Trains skip-gram word2vec and projects the vectors to 2D two ways.

    t-SNE keeps neighbourhoods but distorts distances between clusters. PCA
    is a plain linear projection onto the two directions of most variance,
    so it keeps the global layout but loses most of the detail. PCA runs on
    unit-length vectors, to match the cosine similarity used elsewhere.

    Args:
        sentences: One list of station names per trip.
        seed: Random seed for word2vec, t-SNE and PCA.

    Returns:
        The gensim KeyedVectors; a dict mapping "tsne" and "pca" to arrays
        of 2D points, one row per word in ``KeyedVectors.index_to_key``; and
        the share of variance the two PCA components explain.
    """
    from gensim.models import Word2Vec
    from sklearn.decomposition import PCA
    from sklearn.manifold import TSNE

    model = Word2Vec(
        sentences, vector_size=64, window=5, min_count=2, sg=1, negative=10,
        epochs=40, seed=seed, workers=1,
    )
    vectors = model.wv
    tsne = TSNE(n_components=2, perplexity=30, metric="cosine", init="pca",
                random_state=seed).fit_transform(vectors.vectors)
    pca = PCA(n_components=2, random_state=seed)
    pca_points = pca.fit_transform(vectors.get_normed_vectors())
    return vectors, {"tsne": tsne, "pca": pca_points}, float(pca.explained_variance_ratio_.sum())


def export(vectors, projections: dict, kinds: dict[str, Counter], neighbours: int = 8) -> list[dict]:
    """Turns the trained vectors into records for the plot.

    Args:
        vectors: The gensim KeyedVectors.
        projections: 2D points per projection name, in ``index_to_key``
            order, from ``train_and_project``.
        kinds: Trip kinds per station, from ``station_kinds``.
        neighbours: How many nearest stations to list for each.

    Returns:
        One dict per station with its name, its ``[x, y]`` position under
        each projection, trip counts by kind and nearest stations with
        their cosine similarity.
    """
    records = []
    for i, name in enumerate(vectors.index_to_key):
        records.append({
            "name": name,
            **{key: [round(float(v), 4) for v in points[i]] for key, points in projections.items()},
            "kinds": dict(kinds[name]),
            "near": [[other, round(float(sim), 3)] for other, sim in vectors.most_similar(name, topn=neighbours)],
        })
    return records


def render_html(records: list[dict], meta: dict, template: str) -> str:
    """Fills the plot page template with compact station data.

    Neighbours become indexes into the station list, which roughly halves
    the size of the embedded data.

    Args:
        records: Station records from ``export``.
        meta: Facts shown on the page, e.g. the trip count.
        template: The page source, containing ``/*DATA*/null`` once.

    Returns:
        The page with the data embedded as a JavaScript object.
    """
    index = {record["name"]: i for i, record in enumerate(records)}
    data = {
        "meta": meta,
        "names": [r["name"] for r in records],
        "proj": {key: {"x": [r[key][0] for r in records], "y": [r[key][1] for r in records]}
                 for key in ("tsne", "pca")},
        "kinds": [[r["kinds"].get(k, 0) for k in ("regional", "long-distance", "bus")] for r in records],
        "near": [[[index[n], sim] for n, sim in r["near"]] for r in records],
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return template.replace("/*DATA*/null", payload, 1)


def main() -> None:
    """Reads a database snapshot, trains, and writes the plot data."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("database", type=Path, help="a snapshot of the collector database")
    parser.add_argument("output", type=Path, help="where to write the JSON")
    parser.add_argument("--html", type=Path, help="also write the plot page here")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
    routes = trip_routes(conn)
    vectors, projections, pca_share = train_and_project([stations for stations, _ in routes])
    records = export(vectors, projections, station_kinds(routes))
    args.output.write_text(json.dumps(records, ensure_ascii=False))

    if args.html:
        first, last = conn.execute("SELECT MIN(first_seen_at), MAX(last_updated_at) FROM stops").fetchone()
        meta = {"trips": len(routes), "stations": len(records), "from": first, "to": last,
                "pca_share": round(pca_share, 4)}
        template = (Path(__file__).parent / "station_space.html").read_text()
        args.html.write_text(render_html(records, meta, template))

    print(f"{len(routes)} trips, {len(records)} stations with vectors -> {args.output}")
    for probe in ["Köln Hbf", "München Hbf", "Hamburg Hbf", "Paris Nord"]:
        if probe in vectors:
            print(f"  {probe}: {', '.join(n for n, _ in vectors.most_similar(probe, topn=5))}")


if __name__ == "__main__":
    main()
