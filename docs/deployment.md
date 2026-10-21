# Deployment

The collector runs 24/7 in Docker on our Hetzner VPS (D10, D16).

| What | Where |
|---|---|
| VPS | `ubuntu-4gb-hel1-1`, 46.62.159.140 (Ubuntu 24.04, ARM64, 2 vCPU, 4 GB) |
| Repo checkout | `/opt/db-ml` (owned by `neil`) |
| Credentials | `/opt/db-ml/.env`, `chmod 600`, copied by hand, never in git |
| Data | `/var/lib/db-ml` on the host = `/app/data` in the container |
| Database | `/var/lib/db-ml/db_ml.sqlite` |
| Container user | `collector`, UID/GID 10001, which owns `/var/lib/db-ml` |

The image holds only code. Station reference data (`raw/`, `processed/`) and
the database live in the mounted data folder, so rebuilding or replacing the
container loses nothing. Docker restarts the container if it crashes and
after a reboot (`restart: unless-stopped`). Logs rotate at 5 × 10 MB.

Other things already run on the VPS (`mybot.service`, an `openclaw-gateway`
process). The collector publishes no ports and uses ~100 MB of RAM.

## Everyday commands

Run from `/opt/db-ml` as `neil` (member of the `docker` group):

```sh
docker compose ps                     # is it running?
docker compose logs -f --since 15m    # summary line every 5 minutes
docker compose restart                # stops cleanly within ~1 s (D15)
sqlite3 -readonly /var/lib/db-ml/db_ml.sqlite "SELECT COUNT(*) FROM stops"
```

`sqlite3` needs root, or a user in group 10001, to read the data folder.

A restart costs only the few seconds of downtime: the collector remembers
which plan hours it fetched (D18) and fetches only the ones it missed, so
the `/fchg` catch-up runs within a minute or two. The startup log line says
how many plan hours were fetched before. Only a new database, or downtime
of many hours, means the full ~70-minute plan backfill (D15).

## Deploying a new version

```sh
cd /opt/db-ml
git pull
docker compose up -d --build
```

Schema migrations (D17) run automatically when the collector starts.

## Setting up from scratch

1. Install Docker: `apt-get install docker.io docker-compose-v2 docker-buildx sqlite3`,
   and `usermod -aG docker neil`.
2. `git clone https://github.com/NeilSinclair/db-train-predictor.git /opt/db-ml`
3. Copy `.env` from a laptop: `scp .env neil@<vps>:/opt/db-ml/.env`, then
   `chmod 600 /opt/db-ml/.env`.
4. Create the data folder: `install -d -o 10001 -g 10001 -m 750 /var/lib/db-ml`.
   Set `DB_ML_DATA_DIR` in `.env` to use a different folder.
5. Build the image and the reference data (~5 minutes; StaDa, Wikidata and
   Timetables calls):
   ```sh
   docker compose build
   docker compose run --rm collector sh -ec '
     for m in stations clean_stations city_populations top_stations station_evas db; do
       python -m db_ml.$m
     done'
   ```
   Expect 61 stations and 63 EVA numbers to collect.
6. `docker compose up -d`

To refresh the station list later, rerun step 5's pipeline while the
collector runs, then `docker compose restart` so it picks up the new EVAs.

## Backups

Every night at 03:30 a systemd timer takes a consistent copy of the
database, checks it, and stores it in a Borg repository on our Hetzner
Storage Box (D24). Kept: 14 daily, 8 weekly, 12 monthly archives. Running
since 2026-10-21; the first backup and restore test passed.

| What | Where |
|---|---|
| Latest checked copy (training reads this, D19) | `/var/lib/db-ml-backup/db_ml.sqlite` |
| Scripts and units (sources in `deploy/backup/`) | `/usr/local/sbin/db-ml-backup`, `/usr/local/sbin/db-ml-backup-check`, `/etc/systemd/system/db-ml-backup.{service,timer}` |
| Settings (repository, SSH key) | `/etc/db-ml-backup.env`, `chmod 600` |
| SSH key for the Storage Box | `/root/.ssh/storagebox_ed25519` |
| Repository | `ssh://<user>@<user>.your-storagebox.de:23/./db-ml` |

### Setting it up

1. In the Hetzner Console, open the Storage Box, turn on **SSH support**
   and **external reachability** (needed even from our Hetzner Cloud VPS),
   and set a password. Note its user, e.g. `u123456`. The password is only
   needed during setup; keep it in a password manager, not in the `.env`
   that goes to the VPS.
2. On the VPS, from an interactive terminal:
   ```sh
   cd /opt/db-ml && git pull
   sudo deploy/backup/install.sh u123456
   ```
   It installs Borg, creates an SSH key and asks for the Storage Box
   password once to add the key, creates the repository, enables the
   timer, runs the first backup and then the restore test.

Run `install.sh` again after changing anything in `deploy/backup/`; it
keeps the key, settings and repository.

### Everyday commands

```sh
systemctl list-timers db-ml-backup.timer        # when's the next one?
journalctl -u db-ml-backup --since yesterday    # how did the last one go?
sudo systemctl start db-ml-backup               # back up now
sudo db-ml-backup-check                         # restore test
```

### Restoring

```sh
sudo -i
set -a; . /etc/db-ml-backup.env; set +a
borg list                                       # pick an archive
cd /tmp && borg extract ::db_ml-2026-10-22T03:30
# The file is at /tmp/var/lib/db-ml-backup/db_ml.sqlite. To put it back:
cd /opt/db-ml && docker compose stop
cp /tmp/var/lib/db-ml-backup/db_ml.sqlite /var/lib/db-ml/db_ml.sqlite
rm -f /var/lib/db-ml/db_ml.sqlite-wal /var/lib/db-ml/db_ml.sqlite-shm
chown 10001:10001 /var/lib/db-ml/db_ml.sqlite
docker compose start
```
