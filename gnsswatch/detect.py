"""Deterministic spoofing/jamming detector for ADS-B snapshots. (P2)

No ML, no I/O. Every check is a plain cross-check that does not trust the
aircraft's own GNSS integrity flags (NIC/NACp are used only when present, and
never as the main signal):

  position_jump      implied speed between two fixes is physically impossible
  impossible_speed   reported speed outside airliner range
  baro_geo_mismatch  barometric and GNSS altitude disagree far beyond normal
  velocity_mismatch  reported speed disagrees with the speed the positions imply
  duplicate_id       one icao24 at the same instant in two far-apart places
  integrity_drop     NIC/NACp fall from a good level to a low one (if present)
  signal_dropout     airborne aircraft go silent while neighbours keep reporting

Precision comes first: on-ground aircraft, missing fields, turns, and report
gaps up to about a minute must not fire anything. Thresholds are in
`DetectConfig`. They are engineering judgement; only baro_geo_mismatch and
signal_dropout were adjusted after looking at one real OpenSky sample
(data/snapshots/live_india_sample.json). See NOTES_pranjal.md.

Anomaly.lat/lon (see models.py): for position_jump it is the best estimate of
where the aircraft really was (the last fix before the jump; the fix after it
when the aircraft returns to its true track). For velocity_mismatch it is the
last fix before the mismatch run began. For the other kinds it is the reported
position at `time` (for signal_dropout, the last fix before the silence).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .models import Anomaly, RegionCell, Snapshot

EARTH_R_KM = 6371.0088
_KM_PER_DEG = 111.195


@dataclass
class DetectConfig:
    """All detector thresholds. Speeds m/s, altitudes m, distances km, times s."""

    # impossible_speed
    max_speed_ms: float = 350.0          # ~680 kt ground speed; above this is not an airliner
    min_cruise_speed_ms: float = 40.0    # jets cannot fly this slow at cruise altitude
    cruise_alt_m: float = 7000.0         # low-speed check applies only above this
    # position_jump
    jump_speed_ms: float = 400.0         # implied speed above this is a jump
    jump_min_km: float = 5.0             # ignore small displacements (noise, tiny dt)
    jump_min_km_mlat: float = 25.0       # multilateration fixes are noisier
    # baro_geo_mismatch. Real feeds show a *systematic* GNSS-minus-baro gap of several hundred
    # metres at cruise (weather, geoid) that grows with altitude, so a fixed limit would flag
    # normal traffic. We compare with the aircraft's own median and the regional median in the
    # same altitude band, and flag only when both disagree.
    baro_geo_dev_m: float = 300.0        # deviation from own / regional expectation
    baro_geo_abs_m: float = 1000.0       # safety net: |geo - baro| this large is flagged outright
    baro_geo_bin_m: float = 1500.0       # altitude band for the expectations
    baro_geo_min_samples: int = 6        # own samples in a band to trust the aircraft's median
    baro_geo_min_peers: int = 5          # aircraft in a band to trust the regional median
    baro_geo_lag_s: float = 30.0         # baro and GNSS altitude can be this stale: widen by |vertical rate| x this
    baro_geo_require_pair: bool = True   # one odd sample is a glitch; need two adjacent ones
    # velocity_mismatch
    vel_abs_tol_ms: float = 30.0
    vel_rel_tol: float = 0.25            # tolerance = max(abs, rel * reported speed)
    vel_max_dt_s: float = 60.0           # longer gaps: chord vs arc makes speed unreliable
    vel_min_speed_ms: float = 30.0       # slow movers: ratio too noisy
    vel_turn_skip_deg: float = 15.0      # skip segments where reported track changes this much
    # duplicate_id
    dup_min_km: float = 10.0
    # integrity_drop (NIC/NACp)
    nic_min: int = 6
    nacp_min: int = 6
    integrity_good_min: int = 7          # aircraft must have shown this level first
    integrity_max_severity: float = 0.6  # integrity is one weak signal, never the main one
    # signal_dropout
    dropout_gap_s: float = 90.0
    dropout_cadence_factor: float = 6.0  # also > this many typical report intervals
    dropout_neighbor_km: float = 300.0
    dropout_min_neighbors: int = 2
    dropout_min_history: int = 3         # reports needed before the silence (not a one-off contact)
    dropout_min_cluster: int = 3         # aircraft going silent together; one alone is a coverage gap
    dropout_cluster_km: float = 250.0
    dropout_min_alt_m: float = 3000.0    # low aircraft routinely leave coverage
    dropout_edge_km: float = 40.0        # near the bbox edge = probably left the area
    dropout_max_severity: float = 0.8    # coverage gaps can look the same


# Reliability of each kind when combining per-aircraft evidence.
KIND_WEIGHTS: dict[str, float] = {
    "position_jump": 1.0,
    "duplicate_id": 0.9,
    "baro_geo_mismatch": 0.9,
    "velocity_mismatch": 0.8,
    "impossible_speed": 0.7,
    "signal_dropout": 0.6,
    "integrity_drop": 0.35,
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _hav_km(lat1, lon1, lat2, lon2):
    """Vectorised great-circle distance in km."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dl = np.radians(np.asarray(lon2) - np.asarray(lon1))
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def _sev(ratio: float, sat: float) -> float:
    """Severity 0.3 at the threshold (ratio 1), rising to 1.0 at `sat` times it."""
    x = (ratio - 1.0) / max(sat - 1.0, 1e-9)
    return round(float(0.3 + 0.7 * min(max(x, 0.0), 1.0)), 3)


class _T:
    """Sorted numpy view of a snapshot: samples ordered by (icao24, time)."""

    __slots__ = (
        "n", "icao", "gid", "starts", "gi", "t", "lat", "lon", "baro", "geo", "alt",
        "vel", "trk", "vr", "gnd", "nic", "nacp", "mlat", "pos_ok",
        "same", "dt", "dist", "speed", "seg_ok",
    )


def _prepare(s: Snapshot) -> "_T | None":
    st = s.states
    n = len(st)
    if n == 0:
        return None

    def col(attr: str) -> np.ndarray:
        return np.fromiter(
            (np.nan if (v := getattr(x, attr)) is None else v for x in st), dtype=float, count=n
        )

    icao_raw = np.array([x.icao24 for x in st])
    _, inv = np.unique(icao_raw, return_inverse=True)
    t_raw = np.fromiter((x.time for x in st), dtype=np.int64, count=n)
    order = np.lexsort((t_raw, inv))  # stable: ties keep input order

    T = _T()
    T.n = n
    T.icao = icao_raw[order]
    T.gid = inv[order]
    T.t = t_raw[order]
    T.lat, T.lon = col("lat")[order], col("lon")[order]
    T.baro, T.geo = col("baro_alt_m")[order], col("geo_alt_m")[order]
    T.vel, T.trk = col("velocity_ms")[order], col("track_deg")[order]
    T.vr = col("vertical_rate_ms")[order]
    T.nic, T.nacp = col("nic")[order], col("nacp")[order]
    T.gnd = np.fromiter((x.on_ground for x in st), dtype=bool, count=n)[order]
    T.mlat = np.fromiter((x.position_source == "mlat" for x in st), dtype=bool, count=n)[order]
    T.alt = np.where(np.isfinite(T.baro), T.baro, T.geo)  # prefer baro: GNSS altitude can be spoofed
    T.pos_ok = (
        np.isfinite(T.lat) & np.isfinite(T.lon) & (np.abs(T.lat) <= 90) & (np.abs(T.lon) <= 180)
    )

    # A second fix at the same instant is judged by duplicate_id only; keeping it in the
    # track would distort the next segment's speed.
    T.pos_ok[1:] &= ~((T.gid[1:] == T.gid[:-1]) & (T.t[1:] == T.t[:-1]))

    new_group = np.r_[True, T.gid[1:] != T.gid[:-1]]
    T.starts = np.flatnonzero(new_group)
    T.gi = np.cumsum(new_group) - 1  # group ordinal per sample

    # consecutive-sample segments (k, k+1); length n-1
    T.same = T.gid[1:] == T.gid[:-1]
    T.dt = (T.t[1:] - T.t[:-1]).astype(float)
    T.dist = _hav_km(T.lat[:-1], T.lon[:-1], T.lat[1:], T.lon[1:])
    with np.errstate(divide="ignore", invalid="ignore"):
        T.speed = T.dist * 1000.0 / T.dt
    T.seg_ok = (
        T.same & (T.dt > 0) & ~T.gnd[:-1] & ~T.gnd[1:] & T.pos_ok[:-1] & T.pos_ok[1:]
    )
    return T


def _emit(out: list, T: _T, i: int, kind: str, sev: float, detail: str,
          lat: float | None = None, lon: float | None = None) -> None:
    a = T.alt[i]
    out.append(Anomaly(
        icao24=str(T.icao[i]), time=int(T.t[i]),
        lat=float(T.lat[i] if lat is None else lat), lon=float(T.lon[i] if lon is None else lon),
        alt_m=None if not np.isfinite(a) else float(a),
        kind=kind, severity=float(min(max(sev, 0.0), 1.0)), detail=detail,
    ))


# ---------------------------------------------------------------------------
# per-sample checks
# ---------------------------------------------------------------------------

def _impossible_speed(T: _T, cfg: DetectConfig, out: list) -> None:
    v = T.vel
    fin = np.isfinite(v) & ~T.gnd
    with np.errstate(invalid="ignore"):
        high = fin & (v > cfg.max_speed_ms)
        low = fin & (v > 0) & (v < cfg.min_cruise_speed_ms) & (T.alt >= cfg.cruise_alt_m)
    for i in np.flatnonzero(high):
        _emit(out, T, i, "impossible_speed", _sev(v[i] / cfg.max_speed_ms, 2.0),
              f"reported ground speed {v[i]:.0f} m/s exceeds the airliner limit of {cfg.max_speed_ms:.0f} m/s")
    for i in np.flatnonzero(low):
        _emit(out, T, i, "impossible_speed", _sev(cfg.min_cruise_speed_ms / v[i], 4.0),
              f"reported ground speed {v[i]:.0f} m/s at {T.alt[i]:.0f} m is below any jet's flying speed")


def _baro_geo(T: _T, cfg: DetectConfig, out: list) -> np.ndarray:
    """Flag GNSS-vs-baro altitude outliers. Returns per-sample state for jump-side
    classification: 1 = bad, 0 = fine, -1 = unknown (a field is missing)."""
    d = T.geo - T.baro
    valid = np.isfinite(d) & ~T.gnd
    own = np.full(T.n, np.nan)   # aircraft's own median gap in this altitude band
    peer = np.full(T.n, np.nan)  # median of those medians across aircraft in the band
    vi = np.flatnonzero(valid)
    if vi.size:
        band = np.floor(T.baro[vi] / cfg.baro_geo_bin_m).astype(np.int64) + 1000
        key = T.gi[vi].astype(np.int64) * 100_000 + band
        uk, inv = np.unique(key, return_inverse=True)
        order = np.argsort(inv, kind="stable")
        cuts = np.flatnonzero(np.r_[True, np.diff(inv[order]) != 0])
        edges = np.r_[cuts, len(order)]
        dv = d[vi][order]
        med = np.array([np.median(dv[c0:c1]) for c0, c1 in zip(edges[:-1], edges[1:])])
        cnt = np.diff(edges)
        ukband = uk % 100_000
        peer_med = {}
        for bb in np.unique(ukband):
            m = med[ukband == bb]
            if m.size >= cfg.baro_geo_min_peers:
                peer_med[bb] = np.median(m)
        own[vi] = np.where(cnt >= cfg.baro_geo_min_samples, med, np.nan)[inv]
        peer[vi] = np.array([peer_med.get(bb, np.nan) for bb in ukband])[inv]

    dev_own, dev_peer = np.abs(d - own), np.abs(d - peer)
    has_o, has_p = np.isfinite(own), np.isfinite(peer)
    thr = cfg.baro_geo_dev_m + cfg.baro_geo_lag_s * np.nan_to_num(np.abs(T.vr))  # climbing: sources are staler
    with np.errstate(invalid="ignore"):
        over = np.where(has_o & has_p, (dev_own > thr) & (dev_peer > thr),
                        np.where(has_o, dev_own > thr, np.where(has_p, dev_peer > thr, False)))
        bad = valid & (over | (np.abs(d) > cfg.baro_geo_abs_m + thr - cfg.baro_geo_dev_m))
        dev = np.where(has_o & has_p, np.fmin(dev_own, dev_peer), np.where(has_o, dev_own, dev_peer))
        ratio = np.fmax(np.nan_to_num(dev / thr), np.abs(d) / cfg.baro_geo_abs_m)
    if cfg.baro_geo_require_pair:
        pair = bad[:-1] & bad[1:] & T.same
        keep = np.zeros(T.n, dtype=bool)
        keep[:-1] |= pair
        keep[1:] |= pair
        bad &= keep
    for i in np.flatnonzero(bad):
        ref = "own and regional norm" if has_o[i] and has_p[i] else (
            "own norm" if has_o[i] else "regional norm" if has_p[i] else "absolute limit")
        _emit(out, T, i, "baro_geo_mismatch", _sev(ratio[i], 4.0),
              f"GNSS altitude is {d[i]:+.0f} m from barometric altitude, "
              f"{max(dev[i], 0) if np.isfinite(dev[i]) else abs(d[i]):.0f} m off the {ref}")

    state = np.full(T.n, -1, dtype=np.int8)
    state[valid] = 0
    state[bad] = 1
    return state


def _duplicate_id(T: _T, cfg: DetectConfig, out: list) -> set:
    """Same icao24, same instant, far apart. Returns the group ids involved."""
    same_key = T.same & (T.dt == 0)
    if not same_key.any():
        return set()
    new_run = np.r_[True, ~same_key]
    starts = np.flatnonzero(new_run)
    run_id = np.cumsum(new_run) - 1
    first = starts[run_id]
    dist = _hav_km(T.lat, T.lon, T.lat[first], T.lon[first])
    run_len = np.diff(np.r_[starts, T.n])
    with np.errstate(invalid="ignore"):
        maxd = np.maximum.reduceat(np.nan_to_num(dist), starts)
    bad = np.flatnonzero((run_len >= 2) & (maxd >= cfg.dup_min_km))
    gids = set()
    for r in bad:
        i = starts[r]
        gids.add(int(T.gid[i]))
        _emit(out, T, i, "duplicate_id", _sev(maxd[r] / cfg.dup_min_km, 20.0),
              f"{T.icao[i]} reported at {run_len[r]} positions up to {maxd[r]:.0f} km apart "
              f"at the same time")
    return gids


# ---------------------------------------------------------------------------
# kinematic checks
# ---------------------------------------------------------------------------

def _position_jump(T: _T, cfg: DetectConfig, out: list, alt_state: np.ndarray,
                   skip_gid: set) -> np.ndarray:
    """Impossible implied speed between consecutive fixes. Returns the jump-segment mask."""
    ok = T.seg_ok.copy()
    if skip_gid:
        ok &= ~np.isin(T.gid[:-1], list(skip_gid))
    min_km = np.where(T.mlat[:-1] | T.mlat[1:], cfg.jump_min_km_mlat, cfg.jump_min_km)
    with np.errstate(invalid="ignore"):
        jump = ok & (T.dist >= min_km) & (T.speed > cfg.jump_speed_ms)

    # Which side of a jump is the true track? Altitude cue first (the spoofed side
    # has the bad baro/GNSS offset); otherwise assume regimes alternate: the first
    # jump leaves the true track, the next one returns to it.
    displaced: dict[int, bool] = {}
    for k in np.flatnonzero(jump):
        g = int(T.gid[k])
        a, b = alt_state[k], alt_state[k + 1]
        if a == 1 and b == 0:
            leaving = False
        elif a == 0 and b == 1:
            leaving = True
        else:
            leaving = not displaced.get(g, False)
        displaced[g] = leaving
        i = k + 1
        if leaving:
            lat, lon = T.lat[k], T.lon[k]
            where = "last plausible fix before the jump"
        else:
            lat, lon = T.lat[i], T.lon[i]
            where = "fix after returning to the true track"
        _emit(out, T, i, "position_jump", _sev(T.speed[k] / cfg.jump_speed_ms, 8.0),
              f"position moved {T.dist[k]:.0f} km in {T.dt[k]:.0f} s "
              f"(implied {T.speed[k]:.0f} m/s, airliner max about {cfg.max_speed_ms:.0f}); "
              f"location is the {where}", lat=lat, lon=lon)
    return jump


def _velocity_mismatch(T: _T, cfg: DetectConfig, out: list, jump: np.ndarray,
                       skip_gid: set) -> None:
    """Reported speed vs speed implied by the position change."""
    va, vb = T.vel[:-1], T.vel[1:]
    both = np.isfinite(va) & np.isfinite(vb)
    vref = np.where(both, (va + vb) / 2, np.where(np.isfinite(va), va, vb))
    dtrk = np.abs((T.trk[1:] - T.trk[:-1] + 180.0) % 360.0 - 180.0)  # NaN if a track is missing

    ok = T.seg_ok & ~jump & ~(T.mlat[:-1] | T.mlat[1:]) & (T.dt <= cfg.vel_max_dt_s)
    if skip_gid:
        ok &= ~np.isin(T.gid[:-1], list(skip_gid))
    with np.errstate(invalid="ignore"):
        ok &= np.isfinite(vref) & (vref >= cfg.vel_min_speed_ms)
        ok &= ~(dtrk > cfg.vel_turn_skip_deg)  # turning: chord < arc, speeds legitimately differ
        tol = np.maximum(cfg.vel_abs_tol_ms, cfg.vel_rel_tol * vref)
        diff = np.abs(T.speed - vref)
        hit = ok & (diff > tol)
    if not hit.any():
        return

    # position = last fix before this run of mismatches began (best guess at the true spot)
    idx = np.arange(len(hit))
    prev = np.r_[False, hit[:-1]]
    run_start = np.maximum.accumulate(np.where(hit & ~prev, idx, 0))
    for k in np.flatnonzero(hit):
        r = run_start[k]
        _emit(out, T, k + 1, "velocity_mismatch", _sev(diff[k] / tol[k], 4.0),
              f"reported speed {vref[k]:.0f} m/s but positions imply {T.speed[k]:.0f} m/s; "
              f"location is the last fix before the mismatch began",
              lat=T.lat[r], lon=T.lon[r])


# ---------------------------------------------------------------------------
# integrity and dropout
# ---------------------------------------------------------------------------

def _integrity_drop(T: _T, cfg: DetectConfig, out: list) -> None:
    """NIC/NACp falling from a good level to a low one. Silent when the fields are
    absent, and silent for aircraft that were never good (old transponders send 0)."""
    idx = np.arange(T.n)
    fields = ((T.nic, cfg.nic_min, "NIC"), (T.nacp, cfg.nacp_min, "NACp"))
    drop = np.zeros(T.n, dtype=bool)
    deficit = np.zeros(T.n)
    why: dict[int, list[str]] = {}
    with np.errstate(invalid="ignore"):
        for arr, floor, name in fields:
            good = np.isfinite(arr) & (arr >= cfg.integrity_good_min)
            if not good.any():
                continue
            first_good = np.minimum.reduceat(np.where(good, idx, T.n), T.starts)[T.gi]
            low = np.isfinite(arr) & (arr < floor) & ~T.gnd & (idx > first_good)
            drop |= low
            deficit = np.where(low, np.maximum(deficit, floor - arr), deficit)
            for i in np.flatnonzero(low):
                why.setdefault(int(i), []).append(f"{name} {arr[i]:.0f}")
    for i in np.flatnonzero(drop):
        sev = min(cfg.integrity_max_severity, 0.25 + 0.1 * deficit[i])
        _emit(out, T, i, "integrity_drop", sev,
              f"{' and '.join(why[int(i)])} fell below the usual level "
              f"(a weak signal alone: spoofing can leave integrity flags normal)")


def _signal_dropout(T: _T, s: Snapshot, cfg: DetectConfig, out: list, skip_gid: set) -> None:
    """Airborne aircraft go quiet while aircraft nearby keep reporting."""
    if T.n < 2:
        return
    seg = T.same & (T.dt > 0)
    med_dt = float(np.median(T.dt[seg])) if seg.any() else 0.0
    gap_thr = max(cfg.dropout_gap_s, cfg.dropout_cadence_factor * med_dt)
    t_end = max(int(s.t_end), int(T.t.max()))

    # candidates: (index of last report before silence, silence start, silence end)
    mid = np.flatnonzero(T.same & (T.dt > gap_thr))
    ends = np.r_[T.starts[1:], T.n] - 1
    tail = ends[(t_end - T.t[ends]) > gap_thr]
    cand = [(int(k), int(T.t[k]), int(T.t[k + 1]), False) for k in mid]
    cand += [(int(k), int(T.t[k]), t_end, True) for k in tail]
    if not cand:
        return

    lat0, lon0, lat1, lon1 = s.bbox
    bbox_ok = lat1 > lat0 and lon1 > lon0
    by_time = np.argsort(T.t, kind="stable")
    t_sorted = T.t[by_time]

    kept = []  # (k, ta, tb, is_tail, n_neighbours)
    for k, ta, tb, is_tail in cand:
        if T.gnd[k] or not T.pos_ok[k] or int(T.gid[k]) in skip_gid:
            continue
        if k - T.starts[T.gi[k]] + 1 < cfg.dropout_min_history:
            continue
        if np.isfinite(T.alt[k]) and T.alt[k] < cfg.dropout_min_alt_m:
            continue
        if bbox_ok:
            edge = min(
                (T.lat[k] - lat0) * _KM_PER_DEG, (lat1 - T.lat[k]) * _KM_PER_DEG,
                (T.lon[k] - lon0) * _KM_PER_DEG * np.cos(np.radians(T.lat[k])),
                (lon1 - T.lon[k]) * _KM_PER_DEG * np.cos(np.radians(T.lat[k])),
            )
            if edge < cfg.dropout_edge_km:
                continue
        lo = np.searchsorted(t_sorted, ta, side="right")
        hi = np.searchsorted(t_sorted, tb, side="left")
        j = by_time[lo:hi]
        j = j[(T.gid[j] != T.gid[k]) & T.pos_ok[j]]
        if j.size == 0:
            continue
        near = j[_hav_km(T.lat[k], T.lon[k], T.lat[j], T.lon[j]) <= cfg.dropout_neighbor_km]
        _, counts = np.unique(T.gid[near], return_counts=True)
        n_nb = int((counts >= 2).sum())  # neighbours that really kept reporting
        if n_nb >= cfg.dropout_min_neighbors:
            kept.append((k, ta, tb, is_tail, n_nb))

    # One aircraft going quiet is a coverage gap. Jamming silences several at once, nearby.
    if not kept:
        return
    ks = np.array([c[0] for c in kept])
    ta_a, tb_a = np.array([c[1] for c in kept]), np.array([c[2] for c in kept])
    g_a, la_a, lo_a = T.gid[ks], T.lat[ks], T.lon[ks]
    for c_i, (k, ta, tb, is_tail, n_nb) in enumerate(kept):
        with_me = (g_a != g_a[c_i]) & (ta_a <= tb) & (tb_a >= ta) & (
            _hav_km(la_a[c_i], lo_a[c_i], la_a, lo_a) <= cfg.dropout_cluster_km)
        n_silent = 1 + np.unique(g_a[with_me]).size
        if n_silent < cfg.dropout_min_cluster:
            continue
        gap = tb - ta
        sev = min(_sev(gap / gap_thr, 8.0), cfg.dropout_max_severity)
        kind_txt = "stopped reporting and never resumed" if is_tail else "went silent"
        _emit(out, T, k, "signal_dropout", sev,
              f"airborne aircraft {kind_txt} for {gap:.0f} s (from its last fix) together with "
              f"{n_silent - 1} nearby aircraft, while {n_nb} others within "
              f"{cfg.dropout_neighbor_km:.0f} km kept reporting")


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def detect(s: Snapshot, cfg: "DetectConfig | None" = None) -> list[Anomaly]:
    """Run every check on a snapshot. Deterministic; sorted by (time, icao24, kind)."""
    cfg = cfg or DetectConfig()
    T = _prepare(s)
    if T is None:
        return []
    out: list[Anomaly] = []
    with np.errstate(invalid="ignore", divide="ignore"):
        dup_gids = _duplicate_id(T, cfg, out)
        alt_state = _baro_geo(T, cfg, out)
        jump = _position_jump(T, cfg, out, alt_state, dup_gids)
        _velocity_mismatch(T, cfg, out, jump, dup_gids)
        _impossible_speed(T, cfg, out)
        _integrity_drop(T, cfg, out)
        _signal_dropout(T, s, cfg, out, dup_gids)
    out.sort(key=lambda a: (a.time, a.icao24, a.kind))
    return out


def score_flights(anoms: list[Anomaly]) -> dict[str, float]:
    """Combine each aircraft's anomalies into one 0..1 score (only flagged aircraft appear).

    Per kind: weight x worst severity x persistence (one-off glitches count less than
    repeated evidence). Kinds combine as independent evidence (noisy-OR).
    """
    by: dict[str, dict[str, list[float]]] = {}
    for a in anoms:
        by.setdefault(a.icao24, {}).setdefault(a.kind, []).append(a.severity)
    scores: dict[str, float] = {}
    for icao, kinds in by.items():
        miss = 1.0
        for kind, sevs in kinds.items():
            persist = 0.6 + 0.4 * min(1.0, len(sevs) / 3)
            miss *= 1.0 - KIND_WEIGHTS.get(kind, 0.5) * max(sevs) * persist
        scores[icao] = round(min(max(1.0 - miss, 0.0), 1.0), 4)
    return scores


def region_summary(anoms: list[Anomaly], cell_deg: float = 1.0) -> list[RegionCell]:
    """Bin anomalies into cell_deg x cell_deg cells, busiest first."""
    if cell_deg <= 0:
        raise ValueError("cell_deg must be positive")
    cells: dict[tuple[int, int], dict] = {}
    for a in anoms:
        key = (int(np.floor(a.lat / cell_deg)), int(np.floor(a.lon / cell_deg)))
        c = cells.setdefault(key, {"n": 0, "ac": set(), "sev": 0.0})
        c["n"] += 1
        c["ac"].add(a.icao24)
        c["sev"] = max(c["sev"], a.severity)
    out = [
        RegionCell(lat=round((i + 0.5) * cell_deg, 6), lon=round((j + 0.5) * cell_deg, 6),
                   n_anomalies=c["n"], n_aircraft=len(c["ac"]), max_severity=c["sev"])
        for (i, j), c in cells.items()
    ]
    out.sort(key=lambda r: (-r.n_anomalies, -r.max_severity, r.lat, r.lon))
    return out
