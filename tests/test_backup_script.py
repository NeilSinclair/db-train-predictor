"""Tests for the nightly backup script (deploy/backup/db-ml-backup.sh).

The script runs for real against a temporary database, with a stand-in
``borg`` that only records how it was called, and without switching user.
"""

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from db_ml.db import connect, init_db

SCRIPT = Path(__file__).parents[1] / "deploy" / "backup" / "db-ml-backup.sh"

pytestmark = pytest.mark.skipif(shutil.which("sqlite3") is None, reason="needs the sqlite3 command")


@pytest.fixture
def setup(tmp_path):
    """Provides a database, a backup folder and a stand-in borg.

    Returns:
        A dict with the paths and the environment to run the script with.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    borg_log = tmp_path / "borg.log"
    borg = bin_dir / "borg"
    borg.write_text(f'#!/bin/sh\necho "$@" >> "{borg_log}"\n')
    borg.chmod(0o755)
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    db = tmp_path / "db_ml.sqlite"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "DB_ML_DATABASE": str(db),
        "DB_ML_BACKUP_DIR": str(backup_dir),
        "DB_ML_BACKUP_RUN_AS": "",
    }
    return {"db": db, "copy": backup_dir / "db_ml.sqlite", "borg_log": borg_log, "env": env}


def run(setup) -> subprocess.CompletedProcess:
    """Runs the backup script.

    Args:
        setup: The ``setup`` fixture.

    Returns:
        The finished process, with its output as text.
    """
    return subprocess.run(["bash", str(SCRIPT)], env=setup["env"], capture_output=True, text=True)


def test_backup_copies_the_database_including_uncheckpointed_writes(setup):
    conn = connect(setup["db"])
    init_db(conn)
    conn.execute("PRAGMA wal_autocheckpoint = 0")  # keep the write in the WAL file only
    conn.execute("INSERT INTO stations VALUES (1, 'Köln Hbf', 'Köln', '05315000', NULL, 50.9, 6.9)")
    conn.commit()

    result = run(setup)
    conn.close()

    assert result.returncode == 0, result.stderr
    copy = sqlite3.connect(setup["copy"])
    assert copy.execute("SELECT COUNT(*) FROM stations").fetchone()[0] == 1
    assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    copy.close()
    assert not Path(f"{setup['copy']}.tmp").exists()


def test_backup_stores_the_copy_in_borg_then_thins_out_old_archives(setup):
    init_db(connect(setup["db"]))

    run(setup)

    calls = setup["borg_log"].read_text().splitlines()
    assert [c.split()[0] for c in calls] == ["create", "prune", "compact"]
    assert calls[0].endswith(str(setup["copy"]))
    assert "--keep-daily 14 --keep-weekly 8 --keep-monthly 12" in calls[1]


def test_broken_database_keeps_the_previous_copy_and_skips_borg(setup):
    setup["db"].write_bytes(b"this is not a database" * 100)
    setup["copy"].write_bytes(b"previous copy")

    result = run(setup)

    assert result.returncode != 0
    assert setup["copy"].read_bytes() == b"previous copy"
    assert not setup["borg_log"].exists()
