# NOTES: Pranjal (P2: detector)

## Status
- Done: `gnsswatch/detect.py` (`detect`, `score_flights`, `region_summary`, `DetectConfig`, all 7 kinds) and `tests/test_detect.py` (35 tests).
- `python -m pytest`: 159 passed with p1, p3, p4 merged. `python demo/check_end_to_end.py`: ALL PASS (clean baseline 0 anomalies, 23/23 affected flagged, source error 17.6 km, route LOW 0.00 -> HIGH 1.00).
- Speed: 100k states in about 0.15 s (limit was 2 s).

## Precision / recall on P1's simulator
Aircraft level, 40 aircraft, 10 seeds each, 1800 s. "Affected" = `injected.affected_icao24`. Clean baselines (steps 5/10/12/30/60 s, 10 seeds each, 1 h): 0 anomalies in 50 runs.

| Scenario | Affected | Precision | Recall |
|---|---|---|---|
| teleport, r=150 km, 1000 s | 254 | 1.000 | 1.000 |
| drift | 254 | 1.000 | 0.996 |
| attractor | 254 | 1.000 | 0.996 |
| jam (dropout) | 254 | 1.000 | 0.988 |
| jam, r=60 km, 300 s window | 45 | 1.000 | 0.756 |
| jam, 30 s reporting | 256 | 1.000 | 0.941 |

Read these as optimistic. P1's simulator shifts GNSS altitude by 600-1500 m and leaves velocity untouched, which is an easy target. It says the pipeline works, not how it does against a careful spoofer.

## Real data check (read this)
`data/snapshots/live_india_sample.json` (real OpenSky, 525 states, 102 aircraft, 447 s).
- My first version fired 333 anomalies on it: 318 `baro_geo_mismatch` and 15 `signal_dropout`.
- Cause 1: in real data GNSS altitude minus baro altitude is a systematic **+520 to +780 m at 9-12 km** (308 samples, median +678), about 0 near the ground, and grows with altitude. A fixed 500 m limit flags most normal aircraft. Even after that was handled, 3 climbing/descending aircraft near Mumbai had single samples swinging 300-1000 m. My guess is that the two altitudes come from messages of different age; I did not check the raw feed.
- Cause 2: lone aircraft that leave volunteer-receiver coverage look like dropouts.
- Fix: altitude is compared with the aircraft's own median and the regional median in the same altitude band, and only flagged when both disagree. Tolerance widens with vertical rate, and two adjacent samples are needed. Dropout now needs 3+ aircraft silent together. Result: 0 anomalies on the sample, and the seed-1 simulator check still caught 24/24 in every mode.
- **This is tuning, not validation.** I changed the rules after seeing this one sample, from one region and 7 minutes. `test_shipped_real_sample_stays_quiet` only guards against regressions.
- For P1/P4: the synthetic baseline uses a 15-75 m altitude gap and the simulator injects 600-1500 m. Real cruise gaps in the India sample are already 520-780 m. Please do not claim "baro vs GNSS altitude" as a reliable spoofing check in the README or pitch. In this data it separates spoofed from normal only against the aircraft's own history and its neighbours. Suggest making the baseline gap altitude-dependent (about 6% of altitude).

## Thresholds and why (all in `DetectConfig`)
| Kind | Rule | Why |
|---|---|---|
| position_jump | implied speed > 400 m/s and > 5 km (25 km if MLAT) | above any airliner ground speed with margin; small jumps are noise |
| impossible_speed | reported > 350 m/s; or 0 < v < 40 m/s above 7000 m | jet limit / below flying speed at cruise. Exact 0 is treated as missing |
| baro_geo_mismatch | 300 m (+30 s x vertical rate) off own median AND regional median, 2 adjacent samples; 1000 m safety net | see real data above |
| velocity_mismatch | reported vs implied speed differ by > max(30 m/s, 25%); only gaps <= 60 s; skipped if track changes > 15 deg | chord is shorter than the arc in turns; skips MLAT |
| duplicate_id | same icao24, same second, > 10 km apart | two receivers differ by metres, not 10 km. That aircraft is left out of jump/velocity checks (its track is ambiguous) |
| integrity_drop | NIC or NACp < 6 after the aircraft showed >= 7; severity capped at 0.6 | some transponders send 0 all the time, so a low value alone means nothing. Missing = silent |
| signal_dropout | silent > max(90 s, 6 x typical interval), airborne, >= 3 earlier reports, above 3000 m, > 40 km inside bbox, >= 2 neighbours within 300 km still reporting, >= 3 aircraft silent together within 250 km | a 60 s gap is normal; low, edge and lone cases are coverage, not jamming. Severity capped at 0.8 |

Never flagged: on-ground samples and missing fields (skipped by every check).

## What `Anomaly.lat/lon` means (for P3)
- `position_jump`: the last fix before the jump. When the aircraft returns to its true track, the fix after the jump. Which side is true comes from the altitude cue when there is one, else the jumps are assumed to alternate.
- `velocity_mismatch`: the last fix before the mismatch run started.
- `signal_dropout`: the last fix before the silence (a true position).
- `baro_geo_mismatch`, `impossible_speed`, `duplicate_id`, `integrity_drop`: the reported position, which for a spoofed aircraft is the spoofed one. When locating a source, prefer `position_jump`, `velocity_mismatch` and `signal_dropout`.
- Anomalies are per sample, so one spoofed aircraft can produce hundreds. `region_summary.n_anomalies` counts samples, not events. Use `score_flights` or distinct aircraft for headline numbers.

## Known misses
- A spoofer that keeps positions, velocity and altitude consistent is invisible to these checks. Drift with under about 30 m/s of extra speed passes the velocity check.
- An aircraft first seen already spoofed (no clean fix before): with no altitude cue, entry and exit of the jump can be swapped and the location is off by the spoof offset.
- Jamming that hits an aircraft for under about 90 s, or fewer than 3 aircraft, is not flagged (76% recall in the small-zone case above). Sparse traffic (fewer than 2 neighbours) never triggers dropout.
- Altitude check with a single sample, or with under 6 samples per band and under 5 peers, falls back to the 1000 m limit only.
- `duplicate_id` only matches the same second. Two aircraft sharing an ID with interleaved times show up as position jumps.
- No handling of the antimeridian or poles.
