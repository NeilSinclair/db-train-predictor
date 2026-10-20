"""Tests for choosing main stations."""

import pytest

from db_ml.clean_stations import is_main_station


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Aachen Hbf", True),
        ("Berlin Hauptbahnhof", True),
        ("Leverkusen Mitte", True),
        ("Leverkusen-Küppersteg", False),
        ("Berlin Südkreuz", False),
    ],
)
def test_is_main_station(name, expected):
    assert is_main_station(name) is expected
