# Decision log

Every project decision goes here, newest at the bottom. Each entry says what
we decided, why, and what follows from it. If a decision is reversed, add a
new entry that supersedes the old one rather than editing history.

Statuses: **Accepted** (decided), **Open** (being discussed, not decided yet),
**Superseded** (replaced by a later entry).

---

## D1. Project conventions
- **Date:** 2026-10-20
- **Status:** Accepted
- **Decision:** Python is managed with uv. All code has tests. Python uses
  Google-style docstrings. The frontend is React.
- **Details:** See [CLAUDE.md](../CLAUDE.md).

## D2. Main stations are the stations named "Hbf" or "Hauptbahnhof"
- **Date:** 2026-10-20
- **Status:** Accepted
- **Context:** We pull all stations from the StaDa API (`station-data/v2`)
  and need a manageable set to start with.
- **Decision:** A station is a main station if its name contains `Hbf` or
  `Hauptbahnhof`. The second check exists because StaDa spells Berlin's
  station out in full ("Berlin Hauptbahnhof"); it is the only such station.
- **Consequences:** 134 main stations (`clean_stations.py`).

## D3. City populations come from Wikidata, matched on the municipality key
- **Date:** 2026-10-20
- **Status:** Accepted
- **Context:** We want to rank stations by how big their city is.
- **Decision:** Look up populations on Wikidata (free, no API key). Match on
  the official German municipality key (Amtlicher Gemeindeschlüssel), which
  StaDa provides as `municipalityCode`. Fall back to the city name when the
  key doesn't match. Where Wikidata has several figures, use the most recent.
- **Why:** Matching on the key avoids name mismatches such as "Oldenburg (Oldb)"
  vs "Oldenburg". The name fallback covers keys that changed: Hanau got a new
  key when it became a district-free city on 2026-01-01, and StaDa still has
  the old one.
- **Consequences:** All 133 municipalities matched (`city_populations.py`).

## D4. Start with the main stations of the 60 largest cities
- **Date:** 2026-10-20
- **Status:** Accepted
- **Decision:** Keep the 60 most populous cities. A city with more than one
  main station keeps all of them, so Mönchengladbach contributes both
  Mönchengladbach Hbf and Rheydt Hbf, giving 61 stations. For each station
  we keep only: StaDa station number, EVA number, name, city, population,
  latitude and longitude.
- **Consequences:** `data/processed/top_stations.json` (`top_stations.py`).

## D5. Use JSON files for now, move to SQLite for timetable data
- **Date:** 2026-10-20
- **Status:** Accepted
- **Decision:** Station and city reference data stays in JSON and is joined
  in Python. When we start collecting timetable and delay data, we move to
  SQLite.
- **Why:** The reference data is ~130 rows, so a database adds setup without
  saving work. Timetable data will grow continuously and needs querying by
  station, train and time.

## D6. Verify each station's EVA number against the Timetables API
- **Date:** 2026-10-20
- **Status:** Accepted (follow-up open, see below)
- **Context:** StaDa marks one EVA number per station as `isMain`. For Berlin,
  the Timetables API returns no trains for that number.
- **Decision:** Don't trust StaDa's `isMain` EVA blindly. Check every station's
  EVA numbers against the Timetables `/plan` endpoint.
- **Findings** (checked 2026-10-20, 18:00 hour, all 72 EVA numbers of the 61 stations):
  - Berlin's main EVA (`8011160`) has no trains. Main-line trains are on
    `8098160` ("Berlin Hbf"), S-Bahn on `8089021`. The Timetables
    `/station` lookup also returns `8098160` for Berlin Hbf.
  - Hamburg, München, Frankfurt, Leipzig, Stuttgart and Potsdam have trains on
    their main EVA, plus extra EVA numbers for S-Bahn or underground
    (*tief*) platforms. München also splits two regional platform groups
    (Gl. 27–36, Gl. 5–10) onto separate EVA numbers.
  - Leipzig's `8089318` is not a valid Timetables EVA (HTTP 400).
  - Every other station works with its main EVA.
- **Follow-up:** Resolved by D8 (no S-Bahn). `top_stations.py` still needs
  updating to store the EVA number(s) that actually have trains.

## D7. How to build train journeys from station data
- **Date:** 2026-10-20
- **Status:** Accepted
- **Context:** The Timetables API is per station. To know where a train
  started and how its delay develops along the route, we need to link a
  train's stops across stations.
- **Finding:** Each stop's `id` has the form
  `<trip id>-<YYMMDDHHmm of first departure>-<stop index>`. The first two
  parts are the same at every station on the trip. Verified with ICE 372 on
  2026-10-20: Erfurt (stop 16) → Halle (17) → Berlin (18).
- **To decide:** table design, what to store per stop, how often to poll,
  and how to handle stops at stations we don't collect.
- **Proposal:** six SQLite tables (`stations`, `station_evas`, `stops`,
  `stop_changes`, `stop_messages`, and `journeys` derived later), drawn with
  real example rows at https://claude.ai/artifact/2dpuXf5Ft8pbywym3A2CCw.
- **Decision:** Accept the proposal, including its four suggestions:
  one platform column (split later only if arrival and departure differ);
  collect `stop_messages` from the start; store times with their UTC offset
  (e.g. `2026-10-20T17:18+02:00`); coupled trains are two journeys linked to
  each other.

## D8. Don't collect S-Bahn for now
- **Date:** 2026-10-20
- **Status:** Accepted
- **Context:** S-Bahn trains have their own EVA numbers at some stations
  (see D6), and a few also appear on main EVAs.
- **Decision:** Collect main-line trains only. Skip S-Bahn EVA numbers, and
  skip trains with category `S` even on EVAs we do collect.
- **Why:** With one station per city, we'd see almost none of an S-Bahn
  line's stops (in Berlin, only Hauptbahnhof). S-Bahn delays are interesting
  but need many more stations per city; we may add them later.
- **Consequences:** We collect 63 EVA numbers: the main-line EVA of each of
  the 61 stations (Berlin: `8098160`), plus München's two regional platform
  groups (`8098261` Gl.27–36, `8098262` Gl.5–10), which carry RE/RB trains.
- **Rule used in code** (`station_evas.py`): an EVA is skipped if it has no
  trains or at least 90% of its trains are S-Bahn. A simple majority rule
  was tried first and was wrong: Bonn, Dresden, Essen, Heidelberg,
  Ludwigshafen and Rostock list S-Bahn and main-line trains under the same
  EVA, so it dropped those stations entirely.

## D9. Keep the full history of updates to each stop
- **Date:** 2026-10-20
- **Status:** Accepted
- **Decision:** Store every change we observe for a stop (forecast times,
  platform, cancellation) with the time we saw it, not just the latest value.
- **Why:** The model has to predict from what is known at a given moment,
  such as 30 minutes before departure. Training only on final values would
  leak the answer.

## D10. Collector hosting
- **Date:** 2026-10-20
- **Status:** Accepted
- **Constraints:** The collector must run 24/7, because delays are only
  visible live. The Timetables API allows 60 requests per minute. 63 EVA
  numbers fit at roughly 44 requests a minute (`/rchg` every 90 s per EVA,
  `/plan` and `/fchg` hourly); `/rchg` has not been tested yet.
- **Options considered:** Google Cloud e2-micro, Oracle Cloud Always Free,
  Cloudflare Workers + D1 (100k rows written/day cap), GitHub Actions
  (5-minute minimum, no storage).
- **Decision:** Run the collector on our existing Hetzner VPS.

## D11. SQLite on the VPS's existing disk
- **Date:** 2026-10-20
- **Status:** Accepted
- **Context:** No Postgres is running on the VPS. The VPS has a 40 GB disk.
- **Decision:** Use SQLite (WAL mode) on the VPS's existing disk, with no
  separate volume. The database path is configurable, so it can move to a
  volume later by copying the file. Back up nightly to somewhere off the VPS.
- **Why:** The collector is the only writer and runs on the same machine as
  the database, which is the case SQLite handles best. Nothing to install or
  run. Rough estimate is ~10 GB/year including indexes; to be checked against
  real growth after the first day of collecting.
- **Consequences:** Keep all SQL in one module so a later move to Postgres
  stays contained.

## D12. Add Leverkusen Mitte as a main station
- **Date:** 2026-10-20
- **Status:** Accepted
- **Context:** The code review found that Leverkusen (168k people) was missing
  from the top 60: its main station is "Leverkusen Mitte", so D2's
  `Hbf`/`Hauptbahnhof` name filter skips it. Checked against Wikidata's list
  of cities over 100k, it was the only city above the cutoff we missed.
- **Decision:** Add "Leverkusen Mitte" to the main stations by name
  (`EXTRA_MAIN_STATIONS` in `clean_stations.py`).
- **Consequences:** Leverkusen ranks #49. Heilbronn (#60 before) drops out;
  its EVA stays in the database with `collect = 0`. Still 61 stations and 63
  EVA numbers to collect.
