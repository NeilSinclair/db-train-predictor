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

After a (re)start the plan backfill takes about 70 minutes and the first
`/fchg` waits for it (D15). That's expected.

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

Not set up yet (see [todo.md](todo.md)). Plan: a nightly SQLite online
backup (`sqlite3 <db> ".backup <file>"`, safe while the collector writes)
copied to a Hetzner Storage Box.
