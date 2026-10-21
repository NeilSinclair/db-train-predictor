# To do

What's left to build, plus findings from the code review of 2026-10-20.
Tick items off as they're done; record any decisions they need in
[decisions.md](decisions.md).

**Where we left off (2026-10-20):** steps 1–4 are done: the collector
runs 24/7 in Docker on the Hetzner VPS since 2026-10-20 ~20:52 (D16,
[deployment.md](deployment.md)); restarts are cheap since D18. **Next:
the prediction API (D20, 2026-10-21)** and the nightly backup to the
Hetzner Storage Box, then the checks under "After it has run for a while".

## Done 2026-10-20 (from the code review)

- [x] **`clean_stations.py` in line with CLAUDE.md:** docstrings, and it uses
  the shared `files.py` helpers, so it reads and writes UTF-8 and the output
  keeps umlauts readable. Output data unchanged.
- [x] **Leverkusen added** by name (D12). It ranks #49 and Heilbronn drops
  out. Its one EVA had 16 trains in the two sample hours, none of them S-Bahn.
- [x] **One shared API client with retries** (`api.py`): retries 429, 5xx
  and network errors with doubling backoff, and follows `Retry-After`. Used
  for StaDa, Timetables and Wikidata.
- [x] **Shared JSON helpers** (`files.py`), replacing the four variants.
- [x] **Reloading switches off stale EVAs** (`db.sync_station_evas`). Seen
  working: Heilbronn's EVA went to `collect = 0` when it left the top 60.
- [x] **Dropped `evaNumber` from `top_stations.json`.** Coordinates now fall
  back to any EVA entry that has them, instead of crashing.
- [x] `station_evas.py` samples two hours (08:00 and 17:00). No collect
  decision changed compared with the one-hour run.
- [x] Name fallback in `city_populations.py` only matches within the
  station's federal state (first two digits of the municipality key).
- [x] `city_populations.py` handles `null` mailing address or city.
- [x] `stations.py` raises an error if a response has no `total`.

## Next steps

### Step 2: Turn Timetables XML into rows (done 2026-10-20, see D13)
- [x] Tried `/rchg` against the real API: works; each entry is the stop's
  full current state, same as `/fchg`.
- [x] Parse `/plan`, `/fchg` and `/rchg` (`timetable_xml.py`) and store them
  (`db.save_plan`, `db.save_changes`). Tested against real Frankfurt XML in
  `tests/fixtures/`.
- [x] Times converted to ISO 8601 with UTC offset; the repeated hour on
  25 Oct 2026 is tested.
- [x] Changes for unknown stops: created if the entry has full train details,
  otherwise skipped until the `/plan` row exists.
- [x] Cancellations, reinstated trains, coupled trains (`coupled_with`).
- [x] S-Bahn trains skipped.
- [x] `stop_changes` row only when the state differs from the last one
  (built here rather than in step 3).
- [x] Live run on Frankfurt, Berlin and München: 292 stops, 282 snapshots,
  696 messages; a follow-up `/rchg` added only the stops that changed.

### Step 3: Collector loop (done 2026-10-20, see D15)
- [x] Polling schedule (`collector.py`): `/rchg` every 90 s per EVA (spread
  out), `/plan` for the next 4 hours as they come into view, `/fchg` hourly.
- [x] At startup, `/plan` for the past 12 hours too, in the background.
- [x] One rate limiter on the HTTP client, so retries count too.
- [x] Errors are logged and the loop carries on; requests retry once,
  briefly; failed `/plan` fetches are retried every 5 minutes while their
  hour is in the window. A summary is logged every 5 minutes.
- [x] Restart recovery: startup backfill plus `/fchg` catches up.
- [x] Clean stop on SIGTERM/SIGINT. Run with `uv run db-ml-collector`.

- [ ] Check real data for messages flagged as deleted (`del` attribute, not
  seen in 700+ sampled stops). If they occur, store the flag and update the
  message instead of `INSERT OR IGNORE` (code review, 2026-10-20).

### Step 4: Deploy to the Hetzner VPS (done 2026-10-20, see D16, D17)
- [x] **Schema migrations** (`db.MIGRATIONS`, `PRAGMA user_version`, D17).
  The VPS database started on version 1.
- [x] Docker instead of systemd (D16): `Dockerfile`, `compose.yaml`. Docker
  installed on the VPS; repo at `/opt/db-ml`; `.env` copied by `scp`,
  `chmod 600`; data and database in `/var/lib/db-ml`.
- [x] Reference data built on the VPS: 61 stations, 63 EVA numbers.
- [x] Collector started with `docker compose up -d`.
- [x] Committed and pushed; `/opt/db-ml` on the VPS is a plain checkout of
  `main` again (the changes had first been copied over uncommitted).
- [x] Restarts are cheap: fetched plan hours are kept in `plan_fetches`
  (migration 2, D18).
- [x] D18 deployed to the VPS (one last full plan backfill, since earlier
  fetches weren't recorded).
- [ ] **Nightly backup to the Hetzner Storage Box.** Use SQLite's online
  backup (`sqlite3 <db> ".backup <file>"`), which is safe while the
  collector writes; copying the file directly isn't, in WAL mode. Keep a
  few days of copies, and test a restore.

**Close the collection gaps from [features.md](features.md)** (D22). DB
doesn't keep these, so each day without them is lost:
- [ ] Construction messages' validity dates (`from`, `to`) and priority
  (`pr`) on `stop_messages` (F23).
- [ ] Cancellation time (`clt`) on stops and changes (F24).
- [ ] Operator (`tl o`) on stops (F2), and changed path (`cpth`) for
  diversions (F25).

**After it has run for a while:**
- [ ] After day one, measure database growth against the ~10 GB/year
  estimate (D11) and check free disk space. The 5-minute live run stored
  ~700 snapshots, but that was inflated by first-time snapshots.
- [ ] **Clock change, night of 24/25 Oct 2026:** check that night's data
  against the rules in D14 (forecasts in the repeated 02:00 hour,
  departures after arrivals, one `/plan` fetch for local 02:00).
- [ ] The deleted-message (`del`) check from step 3's list, on real data.

### Later
- [ ] **EDA, about mid-November 2026** (four weeks of data): test the
  feature hypotheses in [features.md](features.md); check that stops with
  no changed time really ran on time; compare delay bands with a plain
  regression (D22).
- [ ] Check whether `/plan` returns tomorrow's hours, and whether
  Open-Meteo keeps archived forecasts (features.md, open questions).
- [ ] Decide where the prediction API runs (D20), planned for 2026-10-21.
- [ ] Set up training as in D19: `model/` workspace member, `trainer`
  compose service with a profile and CPU/memory limits, trained from the
  nightly backup copy.
- [ ] Copy only what the collector needs into its image (an allowlist in
  `.dockerignore`) plus a build-time `import db_ml.collector` check, once
  the repo holds model or frontend code.
- [ ] Journey builder: derive `journeys` from `stops` (D7).
- [ ] Find DB's list of message codes (e.g. code 55 on ICE 372).
- [ ] Delay prediction model.
- [ ] Prediction API.
- [ ] React frontend.
- [ ] Add the pipeline commands to CLAUDE.md.
