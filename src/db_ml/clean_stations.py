"""Picks the main stations out of the full StaDa station list.

Example:
    Write ``data/raw/stations_clean.json``::

        $ uv run python -m db_ml.clean_stations
"""

from pathlib import Path

from db_ml.files import load_json, save_json

STATIONS_PATH = Path('data/raw/stations.json')
CLEAN_STATIONS_PATH = Path('data/raw/stations_clean.json')
# Main stations whose names don't contain "Hbf" (decision D12).
EXTRA_MAIN_STATIONS = {'Leverkusen Mitte'}


def is_main_station(name: str) -> bool:
    """Decides whether a station counts as a main station.

    Args:
        name: The StaDa station name.

    Returns:
        True for names containing "Hbf" or "Hauptbahnhof" (Berlin's is
        spelled out), and for the stations listed in EXTRA_MAIN_STATIONS.
    """
    return 'Hbf' in name or 'Hauptbahnhof' in name or name in EXTRA_MAIN_STATIONS


def main() -> None:
    """Reads all stations, keeps the main stations and saves them."""
    stations = load_json(STATIONS_PATH)

    main_stations = []

    for item in stations:
        if is_main_station(item['name']):
            main_stations.append(item)

    save_json(main_stations, CLEAN_STATIONS_PATH)

    print(f'Number of main_stations is {len(main_stations)}')

if __name__ == "__main__":
    main()
