#!/usr/bin/env bash
# Nightly backup of the collector database to the Hetzner Storage Box (D24).
#
# 1. Takes a consistent copy with SQLite's online backup, which is safe while
#    the collector writes. The copy stays on the VPS: it's the file training
#    reads (D19), so a long training run never holds up the live database.
# 2. Checks the copy's integrity, and only then replaces the previous copy.
# 3. Stores it in a Borg repository on the Storage Box. Borg deduplicates,
#    so a full copy every night only uploads the parts that changed.
# 4. Thins out old archives: 14 daily, 8 weekly, 12 monthly.
#
# Runs as root from db-ml-backup.timer, with BORG_REPO and BORG_RSH from
# /etc/db-ml-backup.env. SQLite runs as the collector's user (10001), so any
# file it creates keeps the collector's ownership.
set -euo pipefail

db=${DB_ML_DATABASE:-/var/lib/db-ml/db_ml.sqlite}
copy_dir=${DB_ML_BACKUP_DIR:-/var/lib/db-ml-backup}
run_as=${DB_ML_BACKUP_RUN_AS-setpriv --reuid=10001 --regid=10001 --clear-groups}

# Runs a command as the collector's user (or as is, if run_as is empty).
as_collector() {
    if [[ -n $run_as ]]; then
        # shellcheck disable=SC2086  # run_as is a command with arguments
        $run_as "$@"
    else
        "$@"
    fi
}

copy=$copy_dir/db_ml.sqlite
tmp=$copy_dir/db_ml.sqlite.tmp

rm -f "$tmp"
as_collector sqlite3 -readonly "$db" ".backup '$tmp'"
# A self-contained file: no -wal or -shm files next to it once it's read.
as_collector sqlite3 "$tmp" "PRAGMA journal_mode = DELETE" > /dev/null
result=$(as_collector sqlite3 "$tmp" "PRAGMA integrity_check")
if [[ $result != ok ]]; then
    echo "Integrity check of the copy failed, keeping the previous one: $result" >&2
    exit 1
fi
mv "$tmp" "$copy"
stops=$(as_collector sqlite3 -readonly "$copy" "SELECT COUNT(*) FROM stops")
echo "Copied $(du -h "$copy" | cut -f1) with $stops stops"

borg create --stats --compression zstd,6 "::db_ml-{now:%Y-%m-%dT%H:%M}" "$copy"
borg prune --list --glob-archives 'db_ml-*' --keep-daily 14 --keep-weekly 8 --keep-monthly 12
borg compact
