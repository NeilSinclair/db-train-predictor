# To do

What's left to build, plus findings from the code review of 2026-10-20.
Tick items off as they're done; record any decisions they need in
[decisions.md](decisions.md).

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

### Step 3: Collector loop
- [ ] Polling schedule: `/plan` hourly for the next 4+ hours (`/fchg` reaches
  a few hours ahead), `/rchg` every ~90 s per EVA, `/fchg` hourly as a
  catch-up.
- [ ] At startup, also fetch `/plan` for today's past hours: `/fchg` reaches
  back 8–12 hours, and those changes need a stop to attach to.
- [ ] One rate limiter shared by all requests (60/minute).
- [ ] Logging, and recovery after a restart (catch up with `/fchg`).

- [ ] Check real data for messages flagged as deleted (`del` attribute, not
  seen in 700+ sampled stops). If they occur, store the flag and update the
  message instead of `INSERT OR IGNORE` (code review, 2026-10-20).

### Step 4: Deploy to the Hetzner VPS
- [ ] Service that restarts on failure (systemd), `.env` on the server,
  database path via `DB_ML_DATABASE_PATH`.
- [ ] A way to change the schema once real data exists (simple numbered
  migrations), since recreating the database won't be an option then.
- [ ] Nightly backup off the VPS (Hetzner Storage Box or local machine).
- [ ] After day one, measure database growth against the ~10 GB/year
  estimate (D11) and check free disk space.

### Later
- [ ] Journey builder: derive `journeys` from `stops` (D7).
- [ ] Find DB's list of message codes (e.g. code 55 on ICE 372).
- [ ] Delay prediction model.
- [ ] Prediction API.
- [ ] React frontend.
- [ ] Add the pipeline commands to CLAUDE.md.
