"""Export real pipeline output for the dashboard to fetch.

The dashboard is a static page, and `server.py` only speaks MCP over stdio, so
there is nothing for a browser to call. This script bridges that gap the cheap
way: it builds the demo snapshots, runs the SAME functions the MCP tools expose
(`analyze_snapshot`, `preflight_brief`) over them, and writes the result to
`data/scenarios.json` next to the page. No HTTP server, no new dependency, no
framework -- `python -m http.server` is still enough to run the demo.

What this file adds on top of the pipeline, and why:

* **Flight levels (`assign_flight_levels`).** `synthetic_baseline` draws each
  aircraft's cruise level at random from about eleven values, so with this much
  traffic some pairs end up co-altitude and converging. That is a collision, not
  a demo. We build the conflict graph (any pair that ever closes inside 5 NM
  laterally) and greedy-colour it, then shift both barometric AND GNSS altitude
  by the same delta so the two stay exactly as far apart as the generator made
  them. The detector must still find nothing in the clean snapshot; `main()`
  asserts that.

* **Episodes (`episodes_from`).** The detector emits one Anomaly per suspicious
  OBSERVATION, so one aircraft sitting in a spoofed cell for ten minutes yields
  around sixty of them. Counting those as sixty findings overstates what
  happened. An episode is one aircraft, one kind, one contiguous run of
  observations; that is the number a human should read. The raw count is kept
  alongside it, never thrown away.

* **Clustering (`cluster_anomalies`).** Both zones run for the whole window, and
  `estimate_source` solves for a SINGLE emitter, so handed everything at once it
  lands on a confident-looking point between the two. Splitting the anomalies
  into spatially separate groups first, using only their positions and never the
  injected truth, lets the real locator run once per source.

Run from the repo root:

    PYTHONPATH=. python demo/dashboard/build_data.py

Regenerate after changing detect.py / locate.py, otherwise the page keeps
showing the previous run's numbers.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GNSSWATCH_OFFLINE", "1")

from gnsswatch import server as S  # noqa: E402
from gnsswatch.airports import AIRPORTS  # noqa: E402
from gnsswatch.data import save_snapshot, synthetic_baseline  # noqa: E402
from gnsswatch.hotzones import route_points  # noqa: E402
from gnsswatch.locate import estimate_source, haversine_km  # noqa: E402
from gnsswatch.models import Snapshot, StateVector  # noqa: E402
from gnsswatch.simulate import inject_spoof  # noqa: E402

OUT = Path(__file__).resolve().parent / "data" / "scenarios.json"

BBOX = (8.0, 68.0, 24.0, 88.0)          # India, same box the seed data uses
N_AIRCRAFT = 120
# Airliners cruise at 200-250 m/s, so how far an aircraft gets across the map
# is set by how long the window is, not by the replay speed. At 80 minutes each
# one covered barely half the box and the traffic looked becalmed however fast
# the loop ran. 2 hours puts a leg at roughly 1600 km, which is most of the box
# and about as long as `_pick_leg` can fit inside it.
DURATION_S = 7200
SEED = 7

CLEAN = "dash_clean"
SPOOF = "dash_spoof"

# Delhi -> Bengaluru runs down lon ~77, through the first interference zone.
ORIGIN, DEST = "VIDP", "VOBL"

# Two SIMULATED spoofing zones, both live for the whole window so both sit on
# the map at a constant size. Different modes so they do not look identical:
# teleport jumps the position outright, drift walks it away gradually.
#
# A jam (dropout) zone was tried here and does not work when it runs the whole
# time: the affected aircraft simply never report, so there is almost nothing
# to localise and the estimate degraded to ~280 km. Jamming needs a window.
ZONE_A = {"lat": 19.5, "lon": 79.5, "radius_km": 380.0, "mode": "teleport"}
ZONE_B = {"lat": 13.5, "lon": 74.0, "radius_km": 320.0, "mode": "drift"}

SEP_NM = 5.0
SEP_KM = SEP_NM * 1.852                  # 5 NM, the usual radar separation minimum
VERT_SEP_M = 300.0                       # ~1000 ft
FL_BASE_M = 8534.0                       # FL280
FL_STEP_M = 304.8                        # 1000 ft
N_LEVELS = 14                            # FL280..FL410, a realistic cruise band
CLUSTER_KM = 450.0                       # spatial split between interference sources
LATLON_DP = 3                            # ~110 m, finer than anything claimed here
# Detection runs on every 10 s sample; the page only needs enough to draw, and
# it interpolates between what it gets. Exporting every second sample halves the
# JSON with no visible difference. Frame indices are divided through to match.
EXPORT_STRIDE = 2
# synthetic_baseline draws cruise speed from 200-250 m/s, a 25% spread that
# reads as "everything moves at the same rate". Each aircraft's track is
# time-warped by a factor in [SPEED_MIN_MULT, 1.0] to widen that to roughly 2x.
# Only slowing down, never speeding up: a factor above 1 would run off the end
# of the generated leg. 0.62 x 200 m/s = 124 m/s, still far above the
# detector's 40 m/s min_cruise_speed_ms.
SPEED_MIN_MULT = 0.62
KM_PER_DEG = 111.32

KIND_LABEL = {
    "position_jump": "Position jump",
    "impossible_speed": "Impossible speed",
    "baro_geo_mismatch": "Baro/GNSS alt mismatch",
    "velocity_mismatch": "Velocity mismatch",
    "duplicate_id": "Duplicate ID",
    "integrity_drop": "Integrity drop",
    "signal_dropout": "Signal dropout",
}

# ---------------------------------------------------------------------------
# separation
# ---------------------------------------------------------------------------

def _tracks_array(snap: Snapshot, step: int, n_frames: int):
    """(icao list, lat[n_ac, n_frames], lon[n_ac, n_frames]) with NaN for gaps."""
    icaos = sorted({st.icao24 for st in snap.states})
    idx = {ic: i for i, ic in enumerate(icaos)}
    lat = np.full((len(icaos), n_frames), np.nan)
    lon = np.full((len(icaos), n_frames), np.nan)
    for st in snap.states:
        fi = (st.time - snap.t_start) // step
        if 0 <= fi < n_frames:
            lat[idx[st.icao24], fi] = st.lat
            lon[idx[st.icao24], fi] = st.lon
    return icaos, lat, lon


def _conflict_pairs(lat, lon, sep_km: float):
    """Pairs whose lateral distance ever drops below sep_km.

    Equirectangular distance, which is well inside its error budget over a box
    this size and a threshold this small.
    """
    n = lat.shape[0]
    kx = np.cos(np.deg2rad(np.nanmean(lat)))
    pairs = []
    for i in range(n):
        dlat = (lat[i + 1:] - lat[i]) * KM_PER_DEG
        dlon = (lon[i + 1:] - lon[i]) * KM_PER_DEG * kx
        d = np.sqrt(dlat ** 2 + dlon ** 2)
        with np.errstate(invalid="ignore"):
            close = np.nanmin(np.where(np.isnan(d), np.inf, d), axis=1)
        for off, dist in enumerate(close):
            if dist < sep_km:
                pairs.append((i, i + 1 + off, float(dist)))
    return pairs


def _bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(np.deg2rad, (a[0], a[1], b[0], b[1]))
    dlon = lon2 - lon1
    y = np.sin(dlon) * np.cos(lat2)
    x = np.cos(lat1) * np.sin(lat2) - np.sin(lat1) * np.cos(lat2) * np.cos(dlon)
    return float((np.rad2deg(np.arctan2(y, x)) + 360.0) % 360.0)


def vary_speeds(snap: Snapshot) -> tuple[Snapshot, dict]:
    """Give each aircraft its own cruise speed by time-warping its track.

    Aircraft i takes the position its original track held at time i*m for a
    per-aircraft m <= 1, so it walks the same great-circle leg more slowly and
    never runs past the end of it.

    Reported velocity and track are then recomputed from the points that
    actually land in the file, exactly as `synthetic_baseline` does, so the
    generator's invariant holds: reported speed always agrees with the speed
    implied by the positions. Break that and `velocity_mismatch` fires all over
    the clean baseline.
    """
    rng = np.random.default_rng(SEED + 1)
    by_ac: dict[str, list[StateVector]] = {}
    for st in snap.states:
        by_ac.setdefault(st.icao24, []).append(st)
    for v in by_ac.values():
        v.sort(key=lambda s: s.time)

    step = None
    new_states: list[StateVector] = []
    speeds = []
    for icao, states in sorted(by_ac.items()):
        n = len(states)
        if n < 2:
            new_states.extend(states)
            continue
        if step is None:
            step = states[1].time - states[0].time

        m = float(rng.uniform(SPEED_MIN_MULT, 1.0))
        lats = [s.lat for s in states]
        lons = [s.lon for s in states]

        pts: list[tuple[float, float]] = []
        for i in range(n):
            src = i * m
            f = min(int(src), n - 2)
            frac = src - f
            pts.append((round(lats[f] + (lats[f + 1] - lats[f]) * frac, 6),
                        round(lons[f] + (lons[f + 1] - lons[f]) * frac, 6)))

        seg_speed, seg_track = [], []
        for i in range(n - 1):
            seg_speed.append(haversine_km(pts[i], pts[i + 1]) * 1000.0 / step)
            seg_track.append(_bearing_deg(pts[i], pts[i + 1]))
        seg_speed.append(seg_speed[-1])
        seg_track.append(seg_track[-1])
        speeds.append(float(np.median(seg_speed)))

        for i, st in enumerate(states):
            new_states.append(st.model_copy(update={
                "lat": pts[i][0],
                "lon": pts[i][1],
                "velocity_ms": round(seg_speed[i], 2),
                "track_deg": round(seg_track[i], 2),
            }))

    new_states.sort(key=lambda s: (s.icao24, s.time))
    stats = {"slowest_ms": round(min(speeds), 1), "fastest_ms": round(max(speeds), 1)}
    return snap.model_copy(update={"states": new_states}), stats


def assign_flight_levels(snap: Snapshot, step: int, n_frames: int) -> tuple[Snapshot, dict]:
    """Re-level the traffic so no two aircraft are ever co-altitude and close.

    Minimal graph colouring would satisfy separation with two levels, which is
    both a dull picture and nothing like real traffic. So instead each aircraft
    takes a RANDOM level from the whole cruise band, restricted to levels none
    of its conflict partners already hold. That spreads 75 aircraft across the
    band while still guaranteeing no pair is ever co-altitude inside 5 NM.

    Both altitudes move by the same delta, so the barometric/GNSS offset the
    generator chose is preserved exactly and the detector stays quiet.
    """
    icaos, lat, lon = _tracks_array(snap, step, n_frames)
    pairs = _conflict_pairs(lat, lon, SEP_KM)

    adj: dict[int, set[int]] = {i: set() for i in range(len(icaos))}
    for i, j, _ in pairs:
        adj[i].add(j)
        adj[j].add(i)

    rng = np.random.default_rng(SEED)
    order = sorted(adj, key=lambda k: -len(adj[k]))     # hardest first
    colour: dict[int, int] = {}
    for i in order:
        used = {colour[j] for j in adj[i] if j in colour}
        free = [c for c in range(N_LEVELS) if c not in used]
        if not free:                                    # band exhausted: extend
            free = [max(used) + 1]
        colour[i] = int(rng.choice(free))
    n_levels = len(set(colour.values()))

    level_of = {ic: FL_BASE_M + colour[i] * FL_STEP_M for i, ic in enumerate(icaos)}

    # one delta per aircraft, applied to both altitudes
    base_baro: dict[str, float] = {}
    for st in snap.states:
        base_baro.setdefault(st.icao24, st.baro_alt_m or 0.0)

    new_states = []
    for st in snap.states:
        delta = level_of[st.icao24] - base_baro[st.icao24]
        new_states.append(st.model_copy(update={
            "baro_alt_m": None if st.baro_alt_m is None else round(st.baro_alt_m + delta, 1),
            "geo_alt_m": None if st.geo_alt_m is None else round(st.geo_alt_m + delta, 1),
        }))

    out = snap.model_copy(update={"states": new_states})
    stats = {
        "conflict_pairs_before": len(pairs),
        "flight_levels_used": n_levels,
        "min_lateral_km": round(min((d for _, _, d in pairs), default=float("inf")), 2),
    }
    return out, stats


def verify_separation(snap: Snapshot, step: int, n_frames: int) -> dict:
    """Confirm every close pair is vertically separated. Zero losses expected."""
    icaos, lat, lon = _tracks_array(snap, step, n_frames)
    alt = {}
    for st in snap.states:
        alt.setdefault(st.icao24, st.baro_alt_m or 0.0)

    losses = []
    for i, j, d in _conflict_pairs(lat, lon, SEP_KM):
        if abs(alt[icaos[i]] - alt[icaos[j]]) < VERT_SEP_M:
            losses.append((icaos[i], icaos[j], round(d, 2)))
    return {"close_pairs": len(_conflict_pairs(lat, lon, SEP_KM)), "separation_losses": losses}


# ---------------------------------------------------------------------------
# episodes
# ---------------------------------------------------------------------------

def episodes_from(anoms, snap: Snapshot, step: int) -> list[dict]:
    """Collapse per-observation anomalies into one entry per continuous run.

    The detector is right to fire on every bad observation. A reader should not
    be shown sixty of them for one aircraft sitting in one spoofed cell, so a
    run of the same kind on the same aircraft with no gap longer than
    GAP_FRAMES becomes a single episode carrying its own peak severity and the
    detector's own wording.
    """
    GAP_FRAMES = 3
    by_key: dict[tuple[str, str], list] = {}
    for a in anoms:
        by_key.setdefault((a.icao24, a.kind), []).append(a)

    out = []
    for (icao, kind), items in by_key.items():
        items.sort(key=lambda a: a.time)
        run = [items[0]]
        for a in items[1:]:
            if (a.time - run[-1].time) // step <= GAP_FRAMES:
                run.append(a)
            else:
                out.append(_episode(icao, kind, run, snap, step))
                run = [a]
        out.append(_episode(icao, kind, run, snap, step))

    out.sort(key=lambda e: e["fi"])
    return out


def _episode(icao: str, kind: str, run, snap: Snapshot, step: int) -> dict:
    peak = max(run, key=lambda a: a.severity)
    return {
        "icao24": icao,
        "kind": kind,
        "fi": (run[0].time - snap.t_start) // step // EXPORT_STRIDE,
        "fi_end": (run[-1].time - snap.t_start) // step // EXPORT_STRIDE,
        "n_obs": len(run),
        "sev": round(peak.severity, 2),
        "lat": round(peak.lat, LATLON_DP),
        "lon": round(peak.lon, LATLON_DP),
        "detail": peak.detail,
    }


def cluster_anomalies(anoms) -> list[list]:
    """Split anomalies into spatially separate groups.

    `estimate_source` solves for a SINGLE emitter. Both zones now run for the
    whole simulation, so handing it everything at once converges on a
    confident-looking point between them (about 610 km from either truth when
    this was first tried). Clustering first is the honest fix: it uses only the
    anomaly positions, never the injected truth, and it discovers how many
    sources there are rather than being told.

    Greedy single-pass clustering against running centroids, which is enough
    for sources this far apart. A real implementation belongs in locate.py --
    see NOTES_ahaan.md, which already flags the single-source assumption.
    """
    clusters: list[dict] = []
    for a in sorted(anoms, key=lambda x: x.time):
        best, best_d = None, CLUSTER_KM
        for c in clusters:
            d = haversine_km((a.lat, a.lon), (c["lat"], c["lon"]))
            if d < best_d:
                best, best_d = c, d
        if best is None:
            clusters.append({"lat": a.lat, "lon": a.lon, "items": [a]})
        else:
            best["items"].append(a)
            n = len(best["items"])
            best["lat"] += (a.lat - best["lat"]) / n
            best["lon"] += (a.lon - best["lon"]) / n
    return [c["items"] for c in clusters if len({x.icao24 for x in c["items"]}) >= 3]


def locate_clusters(anoms) -> list[dict]:
    """Run the real locator once per spatial cluster."""
    out = []
    for items in cluster_anomalies(anoms):
        est = estimate_source(items)
        if est is None:
            continue
        out.append({
            "lat": round(est.lat, 4),
            "lon": round(est.lon, 4),
            "radius_km": round(est.radius_km, 1),
            "confidence": round(est.confidence, 3),
            "method": est.method,
            "n_anomalies": est.n_anomalies,
        })
    return out


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------

def build_snapshots() -> tuple[Snapshot, Snapshot, list[dict], int, int]:
    base = synthetic_baseline(BBOX, n_aircraft=N_AIRCRAFT, duration_s=DURATION_S,
                              seed=SEED, name=CLEAN)
    step = 10
    n_frames = (base.t_end - base.t_start) // step + 1

    # Speeds first: warping the tracks moves the aircraft, so conflicts have to
    # be worked out on the final geometry, not the generator's.
    base, spd = vary_speeds(base)
    print(f"speeds: {spd['slowest_ms']}-{spd['fastest_ms']} m/s "
          f"({round(spd['slowest_ms'] * 1.94402)}-{round(spd['fastest_ms'] * 1.94402)} kt)")

    clean, sep_stats = assign_flight_levels(base, step, n_frames)
    print(f"separation: {sep_stats['conflict_pairs_before']} close pairs, "
          f"{sep_stats['flight_levels_used']} flight levels, "
          f"closest {sep_stats['min_lateral_km']} km")

    check = verify_separation(clean, step, n_frames)
    if check["separation_losses"]:
        raise SystemExit(f"separation not assured: {check['separation_losses'][:5]}")
    print(f"separation verified: {check['close_pairs']} close pairs, 0 losses "
          f"(all vertically separated by >= {VERT_SEP_M:.0f} m)")

    # Both SIMULATED events run for effectively the whole window, so both zones
    # are on screen the entire time at a fixed size rather than appearing and
    # fading. Overlapping in time is what breaks the single-source locator, so
    # localisation is done by spatial cluster instead (see cluster_anomalies).
    # The small inset at each end lets the jammed aircraft report once before
    # they go silent and once after they come back.
    span = base.t_end - base.t_start
    t0, t1 = base.t_start + span // 25, base.t_end - span // 25
    events = []

    s1 = inject_spoof(clean, (ZONE_A["lat"], ZONE_A["lon"]), ZONE_A["radius_km"],
                      t0, t1, mode=ZONE_A["mode"], name=SPOOF)
    events.append({**ZONE_A, "affected": list(s1.injected.affected_icao24),
                   "t_start": s1.injected.t_start, "t_end": s1.injected.t_end})

    s2 = inject_spoof(s1, (ZONE_B["lat"], ZONE_B["lon"]), ZONE_B["radius_km"],
                      t0, t1, mode=ZONE_B["mode"], name=SPOOF)
    events.append({**ZONE_B, "affected": list(s2.injected.affected_icao24),
                   "t_start": s2.injected.t_start, "t_end": s2.injected.t_end})

    save_snapshot(clean)
    save_snapshot(s2)
    stats = {**sep_stats, "close_pairs": check["close_pairs"], "separation_losses": 0,
             "sep_nm": SEP_NM, "vert_sep_ft": round(VERT_SEP_M / 0.3048)}
    return clean, s2, events, step, n_frames, stats


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def _tracks(snap: Snapshot, step: int, n_frames: int) -> list[dict]:
    by_ac: dict[str, dict[int, StateVector]] = {}
    callsigns: dict[str, str] = {}
    for st in snap.states:
        fi = (st.time - snap.t_start) // step
        if 0 <= fi < n_frames:
            by_ac.setdefault(st.icao24, {})[fi] = st
        if st.callsign:
            callsigns.setdefault(st.icao24, st.callsign.strip())

    n_out = (n_frames + EXPORT_STRIDE - 1) // EXPORT_STRIDE
    out = []
    for icao24, frames in sorted(by_ac.items()):
        lat: list = [None] * n_out
        lon: list = [None] * n_out
        for fi, st in frames.items():
            if fi % EXPORT_STRIDE:
                continue
            lat[fi // EXPORT_STRIDE] = round(st.lat, LATLON_DP)
            lon[fi // EXPORT_STRIDE] = round(st.lon, LATLON_DP)
        any_st = next(iter(frames.values()))
        out.append({
            "icao24": icao24,
            "callsign": callsigns.get(icao24) or icao24.upper(),
            "fl": int(round((any_st.baro_alt_m or 0) / FL_STEP_M * 10)),   # FL290 etc
            "lat": lat,
            "lon": lon,
        })
    return out


def _scenario(name: str, snap: Snapshot, step: int, n_frames: int, events: list[dict]) -> dict:
    analysis = S.analyze_snapshot(name)
    pack = S.preflight_brief(ORIGIN, DEST, snapshot=name)

    anoms = S.detect(snap)
    scores = S.score_flights(anoms)
    eps = episodes_from(anoms, snap, step)

    kinds_by_ac: dict[str, dict[str, int]] = {}
    for e in eps:
        kinds_by_ac.setdefault(e["icao24"], {})
        kinds_by_ac[e["icao24"]][e["kind"]] = kinds_by_ac[e["icao24"]].get(e["kind"], 0) + 1

    affected = {ic for ev in events for ic in ev["affected"]} if snap.injected else set()

    aircraft = _tracks(snap, step, n_frames)
    for ac in aircraft:
        sc = scores.get(ac["icao24"])
        ac["score"] = None if sc is None else round(sc, 3)
        ac["kinds"] = kinds_by_ac.get(ac["icao24"], {})
        ac["affected"] = ac["icao24"] in affected

    # Ground truth across BOTH events. analyze_snapshot's own simulation_check
    # only tracks the last injection, because a Snapshot carries one event.
    flagged = set(scores)

    # Localise by spatial cluster, then match each estimate to its nearest true
    # zone purely to SCORE it. The estimates themselves never see the truth.
    ests = locate_clusters(anoms) if events else []
    zones_out = []
    for ev in events:
        near, near_d = None, float("inf")
        for e in ests:
            d = haversine_km((e["lat"], e["lon"]), (ev["lat"], ev["lon"]))
            if d < near_d:
                near, near_d = e, d
        est = {**near, "error_km": round(near_d, 1)} if near else None
        zones_out.append({**{k: v for k, v in ev.items() if k != "affected"},
                          "n_affected": len(ev["affected"]),
                          "estimate": est})

    combined = None
    if affected:
        errs = [z["estimate"]["error_km"] for z in zones_out if z["estimate"]]
        combined = {
            "affected_aircraft": len(affected),
            "affected_and_flagged": len(affected & flagged),
            "flagged_not_affected": len(flagged - affected),
            "zones": len(events),
            "clusters_found": len(ests),
            "zones_located": len(errs),
            "worst_error_km": max(errs) if errs else None,
        }

    return {
        "name": name,
        "is_synthetic": analysis["is_synthetic"],
        "bbox": list(snap.bbox),
        "t_start": snap.t_start,
        "t_end": snap.t_end,
        "step_s": step * EXPORT_STRIDE,
        "n_frames": (n_frames + EXPORT_STRIDE - 1) // EXPORT_STRIDE,
        "counts": {
            **analysis["counts"],
            "n_episodes": len(eps),
            "n_observations": analysis["counts"]["n_anomalies"],
        },
        "aircraft": aircraft,
        "episodes": eps,
        "hot_cells": analysis["hot_cells"],
        "ground_truth": combined,
        "zones": zones_out,
        "brief_text": pack["fallback_text"],
    }


def main() -> int:
    clean, spoof, events, step, n_frames, sep = build_snapshots()

    sc_clean = _scenario(CLEAN, clean, step, n_frames, [])
    if sc_clean["counts"]["n_anomalies"]:
        raise SystemExit(f"clean baseline is not clean: {sc_clean['counts']}")
    sc_spoof = _scenario(SPOOF, spoof, step, n_frames, events)

    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "gnsswatch detector via the MCP tool functions",
        "kind_label": KIND_LABEL,
        "separation": sep,
        "fl_step_m": FL_STEP_M,
        "route_line": [[round(la, LATLON_DP), round(lo, LATLON_DP)]
                       for la, lo in route_points(ORIGIN, DEST, 60)],
        "airports": [
            {"icao": c, "lat": AIRPORTS[c].lat, "lon": AIRPORTS[c].lon, "city": AIRPORTS[c].city}
            for c in (ORIGIN, DEST)
        ],
        "scenarios": {"clean": sc_clean, "spoof": sc_spoof},
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")

    for key, sc in payload["scenarios"].items():
        c = sc["counts"]
        print(f"{key:6s} {sc['name']:12s} {c['n_aircraft']:3d} aircraft  "
              f"{c['n_episodes']:4d} episodes ({c['n_observations']} observations)  "
              f"{len(sc['zones'])} zone(s)")
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
