# To do

What's left to build, plus findings from the code review of 2026-10-20.
Tick items off as they're done; record any decisions they need in
[decisions.md](decisions.md).

## Needs a decision

- [ ] **`clean_stations.py`** was written by hand outside these sessions. It
  still has no module docstring, no docstring on `main`, and opens files
  without `encoding="utf-8"`. (`is_main_station` now has a docstring and
  tests.) Bring the rest in line with CLAUDE.md, or leave it as is?

## Done 2026-10-20 (from the code review)

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

### Step 2: Turn Timetables XML into rows
- [ ] Try `/rchg` against the real API (not tested yet; the request budget in
  D10 depends on it).
- [ ] Parse `/plan`, `/fchg` and `/rchg` into `stops`, `stop_changes` and
  `stop_messages` rows. Test against the real XML saved on 2026-10-20.
- [ ] Convert `YYMMDDHHmm` (German local time) to ISO 8601 with UTC offset.
  Test the clock change on 25 Oct 2026, when 02:00–03:00 happens twice.
- [ ] Handle changes that arrive for stops we have no `/plan` row for yet
  (the foreign key will reject them), and trains added at short notice,
  which carry their full details in the change feed.
- [ ] Parse cancellations (`cs="c"`) and coupled trains (`wings` →
  `coupled_with`).
- [ ] Skip trains with category `S` (D8).

### Step 3: Collector loop
- [ ] Polling schedule: `/plan` hourly for the next few hours, `/rchg` every
  ~90 s per EVA, `/fchg` hourly as a catch-up. Decide how many hours ahead
  to fetch.
- [ ] One rate limiter shared by all requests (60/minute).
- [ ] Write a `stop_changes` row only when something differs from the last
  one.
- [ ] Logging, and recovery after a restart (catch up with `/fchg`).

### Step 4: Deploy to the Hetzner VPS
- [ ] Service that restarts on failure (systemd), `.env` on the server,
  database path via `DB_ML_DATABASE_PATH`.
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
