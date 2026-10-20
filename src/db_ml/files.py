"""Reading and writing the project's JSON data files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    """Loads a UTF-8 JSON file.

    Args:
        path: Path to the JSON file.

    Returns:
        The parsed JSON content.
    """
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(data: Any, path: Path) -> None:
    """Writes data to a UTF-8 JSON file, creating parent directories.

    Non-ASCII characters such as umlauts are written as-is, so the file stays
    readable.

    Args:
        data: JSON-serialisable data.
        path: Destination file path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
