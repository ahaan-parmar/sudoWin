# NOTES — P3 (source locator + hot zones)

Branch `p3-locate`. Files: `gnsswatch/locate.py`, `gnsswatch/hotzones.py`,
`gnsswatch/airports.py`, `data/hotzones_seed.json`, `tests/test_locate.py`,
`tests/test_hotzones.py`. Nothing else touched; `models.py` untouched.

`python -m pytest` from the repo root: **84 passed** (29 locate, 55 hotzones).
No PYTHONPATH needed.

---

## What is implemented

Exactly the contract signatures, no additions to them:

```python
# locate.py
haversine_km(a, b) -> float
estimate_source(anoms) -> SourceEstimate | None

# hotzones.py
build_hotzones(history, cell_deg=1.0) -> dict
save_hotzones(hz, path="data/hotzones.json") -> None
load_hotzones(path="data/hotzones_seed.json") -> dict
route_points(origin, destination, n=50) -> list[tuple[float, float]]
route_risk(origin, destination, hour_utc, hz, corridor_km=100.0) -> RouteRisk
```

Plus helpers the contract did not fix, which P4 may use:
`locate.los_radius_km(alt_m)`, `locate.bearing_deg(a, b)`,
`airports.get(icao) -> Airport | None`, `airports.resolve(str) -> (lat, lon)`,
`airports.list_airports()`, `airports.AIRPORTS`, `hotzones.SEED_PATH`.

**Stdlib only** — `math`, `json`, `os`. No numpy. (numpy on my machine is a
broken MINGW build that segfaults on import; the contract allows numpy but does
not require it, and none of this needs it. One less thing to break at the merge.)

---

## How the locator works

A ground transmitter only affects an aircraft within radio line of sight:

    d_km = 3.57 * (sqrt(h_tx_m) + sqrt(h_aircraft_m)),  h_tx = 10 m

So each affected aircraft puts the source inside a disc centred on itself.
The source is in the **intersection** of those discs. `estimate_source` grid-searches
that intersection over 3 refinement passes (40x40 per pass), scoring each candidate
point by how many distinct aircraft could see it, then returns the centroid of the
best-scoring region with its spread as `radius_km`.

At 10 km altitude the LoS radius is ~368 km, so the discs are big and the
intersection is genuinely wide. That is physics, not a bug — and it is why
`radius_km` matters more than `lat/lon` here.

`method` says which branch produced the answer:

| `method` | meaning |
|---|---|
| `los_intersection` | every aircraft can see the returned region |
| `los_partial_intersection` | best region seen by k of n aircraft (outlier, or >1 source) |
| `weighted_centroid` | no usable overlap; severity-weighted mean, confidence capped at 0.35 |

`confidence` = `0.40*count + 0.35*geometry + 0.25*tightness`, scaled by the
fraction of aircraft that can see the region. The geometry term is
`1 - |mean resultant vector|` of the bearings to the aircraft: aircraft spread
around the source score ~1, aircraft all on one side score ~0. Same idea as GDOP.

Returns `None` for an empty list or fewer than **3 distinct aircraft**
(`locate.MIN_AIRCRAFT`). One or two aircraft cannot constrain a position, and a
confident-looking point from them would be a lie.

Positions used are `Anomaly.lat/lon` as-is. Per `models.py` that is already the
last plausible position *before* a jump, which is exactly what the method needs,
so there is no special-casing by anomaly kind. **P2: please keep that
guarantee** — if `lat/lon` for a `position_jump` were the spoofed position, the
locator would point at the spoof target instead of the transmitter.

---

## Localization error (measured)

Simulator lives inside `tests/test_locate.py` (`simulate_event`): place a source,
scatter aircraft, keep the ones within line of sight, optionally add position
noise. 40 seeds per row, source at 54.70N 20.50E.

| scenario | mean err | median | max | median radius | truth inside radius |
|---|---|---|---|---|---|
| 30 aircraft, 10 km alt, clean | **69.6 km** | 62.0 | 200.0 | 153 km | 40/40 |
| + 25 km position noise | 69.5 km | 61.3 | 139.9 | 155 km | 39/40 |
| + 50 km position noise | 74.3 km | 70.8 | 136.0 | 158 km | 40/40 |
| + 100 km position noise | 77.0 km | 72.2 | 139.9 | 132 km | 37/40 |
| 80 aircraft, 10 km alt | 29.7 km | 22.2 | 115.0 | 68 km | 40/40 |
| 80 aircraft, 900 m alt | **7.1 km** | 6.0 | 19.9 | 15 km | 40/40 |
| 60 aircraft, all within a 90° arc | 198.4 km | 198.4 | 224.4 | 234 km | 40/40 |

Read it this way:

- **Typical case: tens of km, not single km.** ~70 km with 30 cruising aircraft.
  Say "we narrowed it to a ~150 km region", never "we found the jammer".
- **The uncertainty radius is honest.** The true source fell inside the reported
  `radius_km` in 236/240 runs across every scenario, including bad geometry.
  That containment property is the thing I optimised for; a tight-looking radius
  that misses is worse than a wide one that holds.
- **Noise barely matters; geometry and altitude dominate.** 100 km of position
  noise costs ~7 km of accuracy. One-sided geometry costs ~130 km, and
  low-flying aircraft (smaller discs) improve it to ~7 km. If the demo needs an
  impressive number, use aircraft on approach, not at cruise.

---

## Hot zones

`build_hotzones` buckets anomalies by (lat/lon cell, hour of day UTC), counting
distinct aircraft rather than raw reports, and emits `basis="observed"`.

`route_risk` resamples the great circle at ~25 km spacing (50–400 points) and
keeps cells whose centre is within `corridor_km` of the track. Cells with
`hour_utc: null` apply at every hour; otherwise the hour must match. Score:

    score = 1 - PROD(1 - w_i * s_i),   w_i = 1 - 0.5 * (d_i / corridor_km)

so more cells raise the score, an on-track cell counts double one at the corridor
edge, and the score saturates towards 1 instead of being pinned by a single cell.

`route_points` uses spherical interpolation, not lat/lon lerp. That matters on
the Baltic routes: between two points at 60°N the great circle bulges past 62°N,
and a straight-line route would miss hot cells on the real track. There is a test
for it.

### Seed data — ILLUSTRATIVE, NOT MEASURED

`data/hotzones_seed.json`: 13 cells across Baltic, Black Sea, Eastern
Mediterranean and Persian Gulf. **Region selection** reflects areas where
interference has been publicly reported. **Every number in it — counts,
severities, hours — is invented for the demo.** The file says so in its `label`,
`basis` is `"illustrative_seed"`, `route_risk` returns
`basis="illustrative_seed"` for it, and the advice list always ends with a line
saying it is not measured and not for operational planning. Two tests enforce
this. Please do not strip that line in the brief.

`basis` is defensive: anything that is not literally `"observed"` or
`"illustrative_seed"` is treated as illustrative. We never claim a measurement
we cannot back up.

---

## For P4 (server + brief)

```python
from gnsswatch.hotzones import load_hotzones, route_risk
from gnsswatch.locate import estimate_source

hz = load_hotzones()                      # defaults to data/hotzones_seed.json
risk = route_risk("EFHK", "EGLL", 14, hz) # ICAO codes or "lat,lon" strings
est  = estimate_source(anomalies)         # may be None — handle it
```

- `estimate_source` returns `None` on <3 distinct aircraft. That is a normal
  answer, not an error: say "not enough aircraft to locate a source".
- Surface `method` and `radius_km` in the brief, not just `lat/lon`. A
  `weighted_centroid` result is not a fix and should be worded as "anomalies
  centred near …", not "source located at …".
- `route_risk` raises `ValueError` on `hour_utc` outside 0–23, an unknown ICAO,
  or a bad corridor. `load_hotzones` raises `FileNotFoundError` on a missing
  file rather than returning empty zones — a warning tool must not score every
  route as safe because a data file went missing.
- `advice` is already short, factual and deterministic. It is meant to be
  relayed, not rewritten or embellished.

**Demo routes that show the hour mattering** (seed data, 100 km corridor):

| route | quiet hour | hot hour |
|---|---|---|
| `ESSA` → `EPWA` | 0.86 (03Z) | **0.95 (14Z)**, 2 cells |
| `EGLL` → `OMDB` | 0.73 (14Z) | **0.85 (03Z)**, 2 cells |
| `OMDB` → `LTFM` | 0.81 (14Z) | **0.91 (19Z)**, 2 cells |
| `EFHK` → `EPWA` | 0.95 at every hour, 3 cells — the loudest route |
| `VOBL` → `VIDP` | 0.00 — the clean contrast case |

---

## Airports

31 airports (`airports.AIRPORTS`), weighted towards the four reported regions.
Coordinates are airport reference points from the **OurAirports** public-domain
dataset, fetched 2026-09-19 from `davidmegginson.github.io/ourairports-data/airports.csv`
and rounded to 5 dp — not typed by hand, not from memory. A test spot-checks
seven of them against independently known positions.

Two legacy ICAO codes are aliased so old flight plans still resolve:
`OKBK → OKKK` (Kuwait, re-coded 2022) and `LTBA → LTFM` (Istanbul, traffic moved
2019).

---

## Contract friction (working around, not changing anything)

1. **`estimate_source(anoms)` only sees anomalies.** The strongest available
   constraint is the one I cannot use: aircraft that flew through the area and
   were *not* affected bound the source by exclusion, often far more tightly
   than the affected ones bound it by inclusion. With the snapshot I could
   likely halve the error. I did **not** change the signature. If there is time
   after the merge, an *additional* optional function on `integration` —
   something like `estimate_source_with_context(anoms, snapshot)` — would be the
   clean way in, leaving the contract signature intact.
2. **`RegionCell` has no hour field**, so `RouteRisk.cells` loses the hour that
   selected them. The hour is in the hot-zone dict and in the first advice line,
   so the brief can still state it. Not worth unfreezing `models.py` for.
3. **`Anomaly.alt_m` is optional** and the ADS-B feeds often omit it. Missing
   altitude falls back to `DEFAULT_AC_ALT_M = 10000 m`, which sets the disc
   radius at ~368 km. If P1/P2 can populate `alt_m` — even roughly — the fix
   gets materially tighter (see the 900 m row above).

---

## Known limits — please keep these in the pitch

- **Geometry only.** No power, antenna-pattern, terrain or refraction model. The
  discs bound where a transmitter *could* be seen from, not where one of a given
  strength actually reaches. A strong transmitter can affect aircraft past the
  geometric horizon; a weak one may not reach it.
- **One transmitter assumed.** Two sources produce no common intersection; that
  surfaces as `los_partial_intersection` with low confidence, not as an error.
  There is a test for the two-source case.
- **Not a bearing-based fix.** This is a plausible source *region* from public
  ADS-B. It is situational awareness, not a certified operational tool, and it
  cannot tell a transmitter from any other cause that happens to affect the same
  aircraft.
- Anomalies are assumed to share a cause; I do not check that they are close in
  time. Caller should pass one event's anomalies.

## If there is time later

1. Use unaffected aircraft as exclusion constraints (item 1 above) — biggest win
   available, likely halves the error.
2. Weight discs by severity instead of treating every affected aircraft equally.
3. Let `build_hotzones` merge into an existing file so zones accumulate across
   sessions rather than being rebuilt each run.
