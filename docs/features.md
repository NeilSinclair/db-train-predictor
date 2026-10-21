# Model features: hypotheses

What we think will predict a train's delay a day ahead (D22), and whether
we're collecting what each feature needs. Nothing here is tested yet. The
point of the list is to make sure the data is there when we do the
exploratory analysis (EDA). Add to it freely; mark what EDA finds.

## The question and the target

**Question:** how late will the train I'm taking tomorrow be?

**Target:** the arrival at the station where you get off, as one of six
delay bands (D22):

| Band | Meaning |
|---|---|
| on time | under 6 minutes late (DB's own definition of punctual) |
| 6–15 | 6 to under 15 minutes late |
| 15–30 | 15 to under 30 minutes late |
| 30–60 | 30 to under 60 minutes late |
| 60+ | 60 minutes or more late |
| cancelled | the arrival at that station was cancelled |

The delay is the last changed arrival we saw for the stop minus the planned
arrival. A stop where DB never posted a changed time probably ran on time;
EDA should check that before we rely on it.

**Rule for every feature:** computed only from data from before a cutoff,
about 24 hours before the planned departure, because that's all the model
will have when it's used.

**Limit:** we only see delays at the 61 stations we collect, so we can
predict arrivals there, not at small stations along the way.

## Status key

- **Stored**: can be computed from what the collector stores today.
- **Later**: comes from an outside source that keeps its history, so we
  can fetch it when we need it.
- **Gap**: DB doesn't keep it for us. Unless we start storing it, it's lost.

## Features

### The train

| # | Feature | Why we think it matters | Status |
|---|---|---|---|
| F1 | Category (ICE, IC, RE, RB, ...) | Long-distance and regional trains differ a lot | Stored (`stops.category`) |
| F2 | Operator (`tl o`: DB Fernverkehr, private regional operators) | Operators differ in rolling stock and reliability | **Gap** |
| F3 | Delays of the same train number at this station, past 7 and 28 days: median, 90th percentile, share late, share cancelled, number of runs | The most direct evidence about this train | Stored |
| F4 | Delays of the same line or route (all trains between the same endpoints), past 7 days, same summaries | More runs than F3 for regional lines with many trains a day | Stored |
| F5 | Planned minutes from the train's first station to this one | Delay builds up along a route | Stored (planned time minus `trip_start`) |
| F6 | Number of stops before this one | Same, counted in stops | Stored (`route_before`) |
| F7 | Total planned stops on the trip | Longer trips have more chances to go wrong | Stored (`route_before`, `route_after`) |
| F8 | Trip starts outside Germany | Trains from abroad bring their own delays | Later (match the first station against StaDa) |
| F9 | Coupled with another train | Splitting and joining ICEs cause delays | Stored (`coupled_with`) |
| F10 | Planned stop time at this station | A long stop lets a late train catch up | Stored (planned departure minus planned arrival) |

### The station

| # | Feature | Why we think it matters | Status |
|---|---|---|---|
| F11 | All trains' delays at this station, past 7 days | Some stations (e.g. Köln Hbf) cause delays themselves | Stored |
| F12 | Trains planned at this station in that hour | Congestion | Stored (from `/plan`) |
| F13 | Station vectors from station2vec (D21) | Lets stations with few runs borrow from similar ones | Stored (derived) |

### Time

| # | Feature | Why we think it matters | Status |
|---|---|---|---|
| F14 | Day of week | Commuter days and weekend traffic | Stored |
| F15 | Hour of planned departure | Rush hours | Stored |
| F16 | Month | Seasons (leaves on tracks, heat). Not learnable until we have about a year of data | Stored |
| F17 | Public holiday, bridge day, day before a holiday, by state | Different traffic | Later (e.g. the `holidays` package) |
| F18 | School holidays, by state | Fewer commuters, more leisure travel | Later (source to pick) |

### Network and region

| # | Feature | Why we think it matters | Status |
|---|---|---|---|
| F19 | Share late across the whole network, past 7 days and past 24 hours | Bad weeks; problems that persist for days | Stored |
| F20 | Share late in the region where the trip starts, past 7 days | Regional problems spread along routes | Stored for our stations by state; the first station's region needs a StaDa match (Later) |
| F21 | Share cancelled on the route and at the station, past 7 days | Cancellations cluster too | Stored |

### Disruptions known in advance

| # | Feature | Why we think it matters | Status |
|---|---|---|---|
| F22 | Weather forecast along the route: wind, heat over ~30 °C, snow and ice, heavy rain | Storms and heat cause much of the long tail | Later (Open-Meteo or DWD history; check that archived *forecasts* exist, not just observations) |
| F23 | Construction work in force at the station or on the route | Planned works slow trains for weeks | **Gap**: construction messages (`t="h"`, `cat="BAUARBEITEN"`) carry validity dates `from` and `to`, which we don't store |
| F24 | Cancellation already announced by the cutoff | A cancellation known the day before is an input, not a surprise; training must tell the two apart | **Gap**: cancellation time `clt` not stored. `stop_changes.observed_at` only gives when we first saw it |
| F25 | Train diverted (changed path) | Diversions add minutes and skip stops | **Gap**: changed path `cpth` not stored |
| F26 | Strike announced | Strikes are announced days ahead | Later (a hand-kept list; news archives keep the dates) |

## Open questions for later

- Can `/plan` return tomorrow's hours? Making a prediction needs tomorrow's
  planned stop. If it can't, we use the same train number's usual
  timetable.
- How much data is enough for EDA? The 7- and 28-day features need that
  much history first, and weekday effects need several weeks. A first look
  after about four weeks of collecting (mid-November 2026).
