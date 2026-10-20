"""Tests for selecting the main stations of the most populous cities."""

import pytest

from db_ml.top_stations import slim_station, station_coordinates, top_city_stations


def make_station(number: int, name: str, code: str, eva_numbers=None) -> dict:
    """Builds a fake StaDa station record.

    Args:
        number: The StaDa station number.
        name: The station name.
        code: The municipality code.
        eva_numbers: Optional custom ``evaNumbers`` list.

    Returns:
        A dict shaped like a StaDa station record.
    """
    if eva_numbers is None:
        eva_numbers = [{
            "number": 8_000_000 + number,
            "isMain": True,
            "geographicCoordinates": {"type": "Point", "coordinates": [6.5, 50.5]},
        }]
    return {
        "number": number,
        "name": name,
        "municipalityCode": code,
        "evaNumbers": eva_numbers,
        "hasWiFi": True,
    }


def make_city(code: str, city: str, population: int | None) -> dict:
    """Builds a fake city population record.

    Args:
        code: The municipality code.
        city: The city name.
        population: The population, or None if unknown.

    Returns:
        A dict shaped like a city_populations.json record.
    """
    return {"municipalityCode": code, "city": city, "population": population}


def point(longitude: float, latitude: float) -> dict:
    """Builds a GeoJSON point as StaDa stores it.

    Args:
        longitude: The longitude.
        latitude: The latitude.

    Returns:
        A ``geographicCoordinates`` dict.
    """
    return {"type": "Point", "coordinates": [longitude, latitude]}


def test_station_coordinates_prefers_main_entry_and_swaps_order():
    station = make_station(1, "X Hbf", "1", eva_numbers=[
        {"number": 1, "geographicCoordinates": point(1.0, 2.0)},
        {"number": 2, "isMain": True, "geographicCoordinates": point(13.37, 52.52)},
    ])

    assert station_coordinates(station) == (52.52, 13.37)


def test_station_coordinates_falls_back_when_main_entry_has_none():
    station = make_station(1, "X Hbf", "1", eva_numbers=[
        {"number": 1, "isMain": True},
        {"number": 2, "geographicCoordinates": point(6.0, 51.0)},
    ])

    assert station_coordinates(station) == (51.0, 6.0)


@pytest.mark.parametrize("eva_numbers", [[], [{"number": 1, "isMain": True}]])
def test_station_coordinates_raises_without_any(eva_numbers):
    with pytest.raises(ValueError, match="X Hbf"):
        station_coordinates(make_station(1, "X Hbf", "1", eva_numbers=eva_numbers))


def test_slim_station_keeps_only_needed_fields():
    station = make_station(1, "Aachen Hbf", "05334002")
    city = make_city("05334002", "Aachen", 261_000)

    assert slim_station(station, city) == {
        "stationNumber": 1,
        "name": "Aachen Hbf",
        "city": "Aachen",
        "municipalityCode": "05334002",
        "population": 261_000,
        "latitude": 50.5,
        "longitude": 6.5,
    }


def test_top_city_stations_keeps_largest_cities_sorted():
    stations = [
        make_station(1, "Small Hbf", "S"),
        make_station(2, "Big Hbf", "B"),
        make_station(3, "Medium Hbf", "M"),
    ]
    populations = [
        make_city("S", "Small", 100),
        make_city("B", "Big", 10_000),
        make_city("M", "Medium", 1_000),
    ]

    result = top_city_stations(stations, populations, top_n=2)

    assert [s["name"] for s in result] == ["Big Hbf", "Medium Hbf"]


def test_top_city_stations_includes_all_stations_of_a_city():
    stations = [
        make_station(1, "Mönchengladbach Hbf", "05116000"),
        make_station(2, "Rheydt Hbf", "05116000"),
        make_station(3, "Other Hbf", "X"),
    ]
    populations = [
        make_city("05116000", "Mönchengladbach", 260_000),
        make_city("X", "Other", 1),
    ]

    result = top_city_stations(stations, populations, top_n=1)

    assert [s["name"] for s in result] == ["Mönchengladbach Hbf", "Rheydt Hbf"]


def test_top_city_stations_ignores_cities_without_population():
    stations = [make_station(1, "A Hbf", "A"), make_station(2, "B Hbf", "B")]
    populations = [make_city("A", "A", None), make_city("B", "B", 5)]

    result = top_city_stations(stations, populations, top_n=2)

    assert [s["name"] for s in result] == ["B Hbf"]
