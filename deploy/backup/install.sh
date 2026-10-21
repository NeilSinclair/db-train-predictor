#!/usr/bin/env bash
# One-time setup of the nightly backup to a Hetzner Storage Box (D24). Run it
# on the VPS from an interactive terminal, since it asks for the Storage
# Box's password once:
#
#   sudo /opt/db-ml/deploy/backup/install.sh u123456
#
# Safe to run again, e.g. after changing the scripts: existing keys, settings
# and the repository are kept.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo." >&2
    exit 1
fi
user=${1:?Usage: install.sh <Storage Box user, e.g. u123456> [host]}
host=${2:-$user.your-storagebox.de}
here=$(dirname "$(readlink -f "$0")")
key=/root/.ssh/storagebox_ed25519
env_file=/etc/db-ml-backup.env

apt-get install -y -q borgbackup sqlite3
install -d -o 10001 -g 10001 -m 750 /var/lib/db-ml-backup
install -m 755 "$here/db-ml-backup.sh" /usr/local/sbin/db-ml-backup
install -m 755 "$here/db-ml-backup-check.sh" /usr/local/sbin/db-ml-backup-check
install -m 644 "$here/db-ml-backup.service" "$here/db-ml-backup.timer" /etc/systemd/system/

if [[ ! -f $key ]]; then
    install -d -m 700 /root/.ssh
    ssh-keygen -q -t ed25519 -N "" -C "db-ml backup on $(hostname)" -f "$key"
fi
if ! ssh -p 23 -i "$key" -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$user@$host" ls > /dev/null 2>&1; then
    echo "Adding the backup key to the Storage Box. Enter the Storage Box password when asked."
    ssh -p 23 -o StrictHostKeyChecking=accept-new "$user@$host" install-ssh-key < "$key.pub"
fi

if [[ ! -f $env_file ]]; then
    cat > "$env_file" <<EOF
BORG_REPO=ssh://$user@$host:23/./db-ml
BORG_RSH="ssh -i $key -o BatchMode=yes"
BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK=yes
EOF
    chmod 600 "$env_file"
fi
set -a
# shellcheck source=/dev/null
. "$env_file"
set +a
# Not encrypted: the data is public timetable data, and a lost passphrase
# would make every backup useless (D24).
borg info > /dev/null 2>&1 || borg init --encryption=none

systemctl daemon-reload
systemctl enable --now db-ml-backup.timer
echo "Running the first backup..."
systemctl start db-ml-backup.service
journalctl -u db-ml-backup.service --since "-10min" --no-pager -o cat | tail -20
echo
echo "Restore test:"
/usr/local/sbin/db-ml-backup-check
echo
systemctl list-timers db-ml-backup.timer --no-pager
