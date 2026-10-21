#!/usr/bin/env bash
# Restore test (D24): extracts the newest archive from the Storage Box into a
# temporary folder, checks its integrity and prints what it holds. Nothing on
# the VPS is changed. Run as root:
#
#   sudo db-ml-backup-check
set -euo pipefail

set -a
# shellcheck source=/dev/null
. /etc/db-ml-backup.env
set +a

archive=$(borg list --short --last 1 --glob-archives 'db_ml-*')
if [[ -z $archive ]]; then
    echo "No archives in $BORG_REPO" >&2
    exit 1
fi
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

echo "Restoring $archive from $BORG_REPO"
(cd "$work" && borg extract "::$archive")
restored=$(find "$work" -name db_ml.sqlite)

result=$(sqlite3 -readonly "$restored" "PRAGMA integrity_check")
if [[ $result != ok ]]; then
    echo "Integrity check failed: $result" >&2
    exit 1
fi
echo "Integrity: ok"
sqlite3 -readonly "$restored" "
    SELECT 'schema version', user_version FROM pragma_user_version;
    SELECT 'stops', COUNT(*) FROM stops;
    SELECT 'stop_changes', COUNT(*) FROM stop_changes;
    SELECT 'latest update', MAX(last_updated_at) FROM stops;"
