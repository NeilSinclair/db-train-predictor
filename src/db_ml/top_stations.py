"""Builds a slim list of main stations in Germany's most populous cities.

Joins the cleaned stations with the city populations on the municipality
code, keeps the top N cities, and writes only the fields we need: station
number, name, city, municipality code, population and coordinates.

The EVA numbers to collect are deliberately not in this file: StaDa's main
EVA isn't always the one with trains, so they come from
``db_ml.station_evas`` instead (decision D6).

Example:
    Write the top 60 cities' stations to ``data/processed/top_stations.json``::

        $ uv run python -m db_ml.top_stations
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from db_ml.files import load_json, save_json

STATIONS_PATH = Path("data/raw/stations_clean.json")
POPULATIONS_PATH = Path("data/raw/city_populations.json")
DEFAULT_OUTPUT_PATH = Path("data/processed/top_stations.json")
DEFAULT_TOP_N = 60


def station_coordinates(station: dict[str, Any]) -> tuple[float, float]:
    """Finds a station's coordinates.

    Coordinates are stored per EVA number. The entry flagged ``isMain`` is
    preferred; otherwise the first entry that has coordinates is used.

    Args:
        station: A StaDa station record.

    Returns:
        The station's (latitude, longitude).

    Raises:
        ValueError: If none of the station's EVA entries has coordinates.
    """
    eva_numbers = sorted(station.get("evaNumbers") or [], key=lambda e: not e.get("isMain"))
    for eva in eva_numbers:
        if coordinates := eva.get("geographicCoordinates"):
            # GeoJSON stores points as [longitude, latitude].
            longitude, latitude = coordinates["coordinates"]
            return latitude, longitude
    raise ValueError(f"Station {station.get('name')!r} has no coordinates")


def slim_station(station: dict[str, Any], city: dict[str, Any]) -> dict[str, Any]:
    """Reduces a station record to the fields we need.

    Args:
        station: A StaDa station record.
        city: The matching record from the city populations file.

    Returns:
        A dict with the station number, name, city, municipality code,
        population and coordinates.
    """
    latitude, longitude = station_coordinates(station)
    return {
        "stationNumber": station["number"],
        "name": station["name"],
        "city": city["city"],
        "municipalityCode": city["municipalityCode"],
        "population": city["population"],
        "latitude": latitude,
        "longitude": longitude,
    }


def top_city_stations(
    stations: list[dict[str, Any]],
    populations: list[dict[str, Any]],
    top_n: int = DEFAULT_TOP_N,
) -> list[dict[str, Any]]:
    """Selects the stations in the ``top_n`` most populous cities.

    A city with more than one main station (e.g. Mönchengladbach Hbf and
    Rheydt Hbf) counts once towards ``top_n`` but contributes all its
    stations, so the result can be slightly longer than ``top_n``.

    Args:
        stations: StaDa station records.
        populations: City records with ``municipalityCode`` and
            ``population`` keys.
        top_n: How many cities to keep.

    Returns:
        Slim station records, sorted by city population (largest first).
    """
    ranked = sorted(
        (p for p in populations if p.get("population") is not None),
        key=lambda p: p["population"],
        reverse=True,
    )
    top_cities = {p["municipalityCode"]: p for p in ranked[:top_n]}

    result = [
        slim_station(station, top_cities[station["municipalityCode"]])
        for station in stations
        if station.get("municipalityCode") in top_cities
    ]
    result.sort(key=lambda s: (-s["population"], s["name"]))
    return result


def main() -> None:
    """Writes the stations of the top 60 cities to the processed data folder."""
    top = top_city_stations(load_json(STATIONS_PATH), load_json(POPULATIONS_PATH))
    save_json(top, DEFAULT_OUTPUT_PATH)
    print(f"Saved {len(top)} stations to {DEFAULT_OUTPUT_PATH}")


if __name__ == "__main__":
    main()
