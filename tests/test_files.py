"""Tests for the JSON file helpers."""

from db_ml.files import load_json, save_json


def test_save_and_load_round_trip_creating_folders(tmp_path):
    path = tmp_path / "nested" / "data.json"
    data = [{"name": "München Hbf", "population": 1_505_036}]

    save_json(data, path)

    assert load_json(path) == data


def test_save_json_keeps_umlauts_readable(tmp_path):
    path = tmp_path / "data.json"

    save_json({"name": "Köln Hbf"}, path)

    assert "Köln" in path.read_text(encoding="utf-8")
