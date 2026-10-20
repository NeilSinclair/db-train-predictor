import json

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
    with open('data/raw/stations.json') as f:
        stations = json.load(f)

    main_stations = []

    for item in stations:
        if is_main_station(item['name']):
            main_stations.append(item)

    with open('data/raw/stations_clean.json', 'w') as f:
        json.dump(main_stations, f, indent=2)

    print(f'Number of main_stations is {len(main_stations)}')

if __name__ == "__main__":
    main()
