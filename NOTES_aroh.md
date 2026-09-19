# P1 notes: data + simulator

Branch `p1-data`. Files: `gnsswatch/data.py`, `gnsswatch/simulate.py`,
`data/snapshots/*`, `tests/test_data.py`, `tests/test_simulate.py`.
All 37 tests pass with `python -m pytest` from the repo root.

## What shipped

`synthetic_baseline` builds level great-circle cruise traffic at 200-250 m/s,
9-12 km, inside the bbox. It produces zero anomalies against every signal
listed in CONTRACT.md. `save_snapshot` / `load_snapshot` / `list_snapshots`
store JSON in `data/snapshots/`. `inject_spoof` has the three modes and
`inject_jam` drops reports. `fetch_live` polls OpenSky over OAuth2.

Committed snapshots, all offline-usable:

| file | kind | states | size | note |
|---|---|---|---|---|
| `baseline_clean.json` | synthetic | 7240 | 1.8 MB | 40 aircraft, 30 min, 10 s step, seed 0. Zero anomalies. |
| `demo_spoof_teleport.json` | synthetic | 7240 | 1.8 MB | Teleport spoof on the baseline, 15 aircraft affected, `injected` holds the ground truth. |
| `live_india_sample.json` | real | 525 | 0.13 MB | 102 real aircraft over the bbox, 8 polls at 15 s. |

## Gotchas the rest of the team needs

**Real barometric and GNSS altitude are hundreds of metres apart.** This is the
big one. In `live_india_sample.json` the median `geo_alt_m - baro_alt_m` is
**+625 m**, p99 is **+770 m**, and the full range is -823 m to +777 m. 76% of
real reports exceed a 200 m threshold. Barometric altitude is pressure altitude
referenced to 1013.25 hPa, so it is not a height at all, and the gap moves with
the weather. A flat `baro_geo_mismatch` threshold below about 900 m will flag
most of the sky on real data. Either set it high, or subtract a per-aircraft or
per-region median offset first and flag the residual. The synthetic baseline
uses an offset of tens of metres, as specified in my brief, so it is cleaner
than reality on this one signal. Do not calibrate the threshold on the
baseline alone.

**Injected spoof altitude offsets are 600-1500 m** for exactly that reason, so
the signal clears whatever threshold survives real data. Position jump and
velocity mismatch are far stronger signals on the demo snapshot anyway.

**Real data is messy.** `live_india_sample.json` also shows 21 dropout gaps,
9 out-of-range speeds and 10 track mismatches under loose thresholds. None of
that is interference, it is just a public feed with partial coverage. The
detector needs to be tolerant or real snapshots will look alarming.

**NIC and NACp are absent from the OpenSky feed**, so they are `None` in every
real snapshot. The synthetic baseline sets `nic=8, nacp=9`. Detectors must
work when both are missing, as the contract says.

**Synthetic time is fixed, not wall clock.** `BASE_TIME = 1789797600`
(2026-09-19 06:00:00 UTC). Using `time.time()` would make the baseline
different on every run, which breaks seeded reproducibility and makes the
committed files churn on every regeneration. If a tool compares snapshot times
against "now", read the times off the snapshot instead of assuming they are
recent.

**Snapshots resolve from the repo root**, not the current directory, so the MCP
server finds them whatever it is launched from. `GNSSWATCH_DATA_DIR` overrides
it, which is what the tests use.

## Design decisions worth knowing

Reported `velocity_ms` and `track_deg` in the baseline are computed from the
positions that actually get written to the file, after rounding to 6 decimal
places, not from the ideal great circle. That is why `velocity_mismatch` cannot
fire on clean data: measured agreement is within 0.005 m/s and 0.005 degrees.

The injectors gate per state, on the true position being inside `radius_km` and
the time being inside the window. An aircraft that flies out of the footprint
mid-window snaps back to its true position. That is a real effect (the receiver
reacquires) and shows up as a second position jump, so do not treat it as a
bug. It also means an affected aircraft's last in-window report may be
untouched.

Under spoofing, `baro_alt_m`, `velocity_ms` and `track_deg` keep their true
values, because the aircraft really is still flying its original path. That
mismatch against the moved positions is the whole detection story. Integrity
fields are left alone on purpose: spoofing can leave them looking normal, which
is the point the contract makes about not leaning on them.

`drift` ramps from each aircraft's own capture time rather than from the global
`t_start`, so an aircraft entering the footprint late drifts from zero instead
of appearing already displaced.

## OpenSky

Basic auth is gone. It is OAuth2 client credentials only, token endpoint
`https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token`.
Set `OPENSKY_CLIENT_ID` and `OPENSKY_CLIENT_SECRET` in the environment. Nothing
is hardcoded and `.env` is already gitignored.

The committed real snapshot was collected on the anonymous tier, which still
answers `/states/all` on a small daily credit allowance. `fetch_live` falls
back to anonymous when credentials are missing, drops the auth header on 401 or
403, honours `Retry-After` on 429 and 503 with exponential backoff, skips
failed polls, and returns an empty snapshot rather than raising if every poll
fails. So the demo never dies on a bad conference network.

Anonymous polling gives roughly 10 s time resolution and one position per
aircraft per request. In the sample, 71 of 102 aircraft got 3 or more reports
over 8 polls, which is enough for short tracks. More polls means better tracks
and more credits burned.

## Deviations from the brief

1. **`demo_spoof_teleport.json` is committed too.** The brief listed a clean
   baseline plus a real snapshot. I added a pre-injected spoof snapshot so the
   demo has a ready spoofed case even if something goes wrong in the merge.
   It is 1.8 MB, well under the limit. Drop it if the repo needs to be smaller.
2. **Spoof altitude offset raised to 600-1500 m** from the few hundred I first
   used, for the pressure altitude reason above.
3. **Aircraft can leave the bbox** if the bbox is smaller than the flight leg
   (speed times duration). Leg selection rejects starts and bearings that would
   exit, and falls back to a centre-out leg when the bbox is genuinely too
   small. States are never truncated at the boundary, because truncating would
   look exactly like a jamming dropout and dirty the baseline. With the demo
   bbox every aircraft stays inside, and there is a test for that.

## Things I did not do

- Barometric altitude is continuous. Real ADS-B quantises it to 25 ft steps.
  Adding that would put 7.6 m jumps in the baseline for no demo benefit.
- No climb or descent profiles, no turns. Everything is level cruise on a
  straight great circle. Enough for the demo, and it keeps the clean baseline
  provably clean.
- `models.py` untouched, as frozen. I needed nothing from it that was missing.
- Geometry helpers (`_haversine_km`, `_destination`, `_bearing_deg`) are
  private in `data.py` because the contract says not to depend on another
  person's module. P3 owns the public `haversine_km`. They will be duplicated
  after the merge. That is deliberate, not an oversight.
