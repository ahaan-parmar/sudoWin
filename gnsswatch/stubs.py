"""Stand-in implementations of every CONTRACT.md function (P1-P3).

server.py uses these only when the real module is missing (or GNSSWATCH_STUBS=1).
They are deliberately simple but behave plausibly so the demo runs end to end.
Snapshots are kept in memory; nothing is written to data/snapshots.
"""
from __future__ import annotations

import json
import math
import random

from gnsswatch.models import (
    Anomaly, Bbox, InjectedEvent, RegionCell, RouteRisk, Snapshot, SourceEstimate, StateVector,
)

_STORE: dict[str, Snapshot] = {}
_STUB_BBOX: Bbox = (54.0, 19.0, 61.0, 28.0)
_T0 = 1_789_000_000


# ---- data.py (P1) ----------------------------------------------------------

def synthetic_baseline(bbox: Bbox, n_aircraft: int = 40, duration_s: int = 1800,
                       step_s: int = 10, seed: int = 0, name: str = "baseline") -> Snapshot:
    rng = random.Random(seed)
    lat0, lon0, lat1, lon1 = bbox
    states: list[StateVector] = []
    for i in range(n_aircraft):
        lat, lon = rng.uniform(lat0, lat1), rng.uniform(lon0, lon1)
        track, speed, alt = rng.uniform(0, 360), rng.uniform(200, 250), rng.uniform(9000, 11500)
        for t in range(0, duration_s + 1, step_s):
            d_km = speed * t / 1000
            la = lat + d_km * math.cos(math.radians(track)) / 111.0
            lo = lon + d_km * math.sin(math.radians(track)) / (111.0 * math.cos(math.radians(la)))
            states.append(StateVector(
                icao24=f"stub{i:02d}", callsign=f"STB{i:03d}", time=_T0 + t, lat=la, lon=lo,
                baro_alt_m=alt, geo_alt_m=alt + 60, velocity_ms=speed, track_deg=track,
            ))
    return Snapshot(name=name, bbox=bbox, t_start=_T0, t_end=_T0 + duration_s,
                    states=states, kind="synthetic")


def fetch_live(bbox: Bbox, polls: int = 8, interval_s: int = 12, name: str = "live") -> Snapshot:
    return synthetic_baseline(bbox, duration_s=polls * interval_s, step_s=interval_s, name=name)


def save_snapshot(s: Snapshot, path: str | None = None) -> str:
    _STORE[s.name] = s
    return path or f"memory://{s.name}"


def load_snapshot(name_or_path: str) -> Snapshot:
    if name_or_path not in _STORE and name_or_path == "baseline":
        save_snapshot(synthetic_baseline(_STUB_BBOX))
    if name_or_path not in _STORE:
        raise FileNotFoundError(f"no snapshot named {name_or_path!r}")
    return _STORE[name_or_path]


def list_snapshots() -> list[str]:
    load_snapshot("baseline")
    return sorted(_STORE)


# ---- simulate.py (P1) ------------------------------------------------------

def _affected(s: Snapshot, source, radius_km, t_start, t_end) -> set[str]:
    return {st.icao24 for st in s.states
            if t_start <= st.time <= t_end and haversine_km((st.lat, st.lon), source) <= radius_km}


def inject_spoof(s: Snapshot, source: tuple[float, float], radius_km: float,
                 t_start: int, t_end: int, mode: str = "teleport",
                 target: tuple[float, float] | None = None, seed: int = 0,
                 name: str | None = None) -> Snapshot:
    target = target or (source[0] + 1.5, source[1] - 2.0)
    hit = _affected(s, source, radius_km, t_start, t_end)
    out = []
    for st in s.states:
        if st.icao24 in hit and t_start <= st.time <= t_end:
            frac = (st.time - t_start) / max(1, t_end - t_start)
            w = 1.0 if mode in ("teleport", "attractor") else frac
            st = st.model_copy(update={
                "lat": st.lat + w * (target[0] - st.lat), "lon": st.lon + w * (target[1] - st.lon),
                "geo_alt_m": (st.geo_alt_m or 0) + 900 * w,
            })
        out.append(st)
    ev = InjectedEvent(mode=mode, source_lat=source[0], source_lon=source[1], radius_km=radius_km,
                       t_start=t_start, t_end=t_end, affected_icao24=sorted(hit),
                       target_lat=target[0], target_lon=target[1])
    return s.model_copy(update={"name": name or f"{s.name}_spoof_{mode}", "states": out,
                                "kind": "synthetic", "injected": ev})


def inject_jam(s: Snapshot, source: tuple[float, float], radius_km: float,
               t_start: int, t_end: int, seed: int = 0, name: str | None = None) -> Snapshot:
    hit = _affected(s, source, radius_km, t_start, t_end)
    out = [st for st in s.states if not (st.icao24 in hit and t_start <= st.time <= t_end)]
    ev = InjectedEvent(mode="dropout", source_lat=source[0], source_lon=source[1],
                       radius_km=radius_km, t_start=t_start, t_end=t_end, affected_icao24=sorted(hit))
    return s.model_copy(update={"name": name or f"{s.name}_jam", "states": out,
                                "kind": "synthetic", "injected": ev})


# ---- detect.py (P2) --------------------------------------------------------

def detect(s: Snapshot, cfg=None) -> list[Anomaly]:
    by_id: dict[str, list[StateVector]] = {}
    for st in s.states:
        by_id.setdefault(st.icao24, []).append(st)
    anoms: list[Anomaly] = []
    for icao, sts in by_id.items():
        sts.sort(key=lambda x: x.time)
        last_ok = sts[0]
        for prev, cur in zip(sts, sts[1:]):
            dt = cur.time - prev.time
            v = haversine_km((prev.lat, prev.lon), (cur.lat, cur.lon)) * 1000 / max(dt, 1)
            if v > 400:
                anoms.append(Anomaly(icao24=icao, time=cur.time, lat=last_ok.lat, lon=last_ok.lon,
                                     alt_m=cur.baro_alt_m, kind="position_jump", severity=0.9,
                                     detail=f"implied speed {v:.0f} m/s"))
            else:
                last_ok = cur
            if dt > 60 and not cur.on_ground:
                anoms.append(Anomaly(icao24=icao, time=prev.time, lat=prev.lat, lon=prev.lon,
                                     alt_m=prev.baro_alt_m, kind="signal_dropout", severity=0.6,
                                     detail=f"no reports for {dt} s"))
        for st in sts:
            if st.baro_alt_m is not None and st.geo_alt_m is not None \
                    and abs(st.geo_alt_m - st.baro_alt_m) > 400:
                anoms.append(Anomaly(icao24=icao, time=st.time, lat=st.lat, lon=st.lon,
                                     alt_m=st.baro_alt_m, kind="baro_geo_mismatch", severity=0.7,
                                     detail=f"geo-baro {st.geo_alt_m - st.baro_alt_m:.0f} m"))
    return anoms


def score_flights(anoms: list[Anomaly]) -> dict[str, float]:
    out: dict[str, float] = {}
    for a in anoms:
        out[a.icao24] = max(out.get(a.icao24, 0.0), a.severity)
    return out


def region_summary(anoms: list[Anomaly], cell_deg: float = 1.0) -> list[RegionCell]:
    cells: dict[tuple[int, int], list[Anomaly]] = {}
    for a in anoms:
        cells.setdefault((math.floor(a.lat / cell_deg), math.floor(a.lon / cell_deg)), []).append(a)
    return [RegionCell(lat=(i + 0.5) * cell_deg, lon=(j + 0.5) * cell_deg, n_anomalies=len(v),
                       n_aircraft=len({a.icao24 for a in v}), max_severity=max(a.severity for a in v))
            for (i, j), v in sorted(cells.items(), key=lambda kv: -len(kv[1]))]


# ---- locate.py (P3) --------------------------------------------------------

def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def estimate_source(anoms: list[Anomaly]) -> SourceEstimate | None:
    true_pos = [a for a in anoms if a.kind in ("position_jump", "signal_dropout")]
    anoms = true_pos or anoms
    if len(anoms) < 3:
        return None
    lat = sum(a.lat for a in anoms) / len(anoms)
    lon = sum(a.lon for a in anoms) / len(anoms)
    spread = sum(haversine_km((a.lat, a.lon), (lat, lon)) for a in anoms) / len(anoms)
    return SourceEstimate(lat=lat, lon=lon, radius_km=max(20.0, spread), confidence=min(1.0, len(anoms) / 30),
                          n_anomalies=len(anoms), method="stub: centroid of anomaly positions")


# ---- airports.py / hotzones.py (P3) ----------------------------------------

AIRPORTS = {
    "EYVI": (54.634, 25.286), "EVRA": (56.924, 23.971), "EETN": (59.413, 24.833),
    "EFHK": (60.317, 24.963), "ESSA": (59.652, 17.919), "EPWA": (52.166, 20.967),
    "LCLK": (34.875, 33.625), "LLBG": (32.011, 34.887), "OMDB": (25.253, 55.364),
    "VOBL": (13.199, 77.706), "VIDP": (28.566, 77.103),
}


def _point(p: str) -> tuple[float, float]:
    p = p.strip().upper()
    if p in AIRPORTS:
        return AIRPORTS[p]
    lat, lon = (float(x) for x in p.split(","))
    return lat, lon


def route_points(origin: str, destination: str, n: int = 50) -> list[tuple[float, float]]:
    (a0, o0), (a1, o1) = _point(origin), _point(destination)
    return [(a0 + (a1 - a0) * k / (n - 1), o0 + (o1 - o0) * k / (n - 1)) for k in range(n)]


def build_hotzones(history: list[Anomaly], cell_deg: float = 1.0) -> dict:
    return {"basis": "observed", "cell_deg": cell_deg,
            "cells": [c.model_dump() for c in region_summary(history, cell_deg)]}


def save_hotzones(hz: dict, path: str = "data/hotzones.json") -> None:
    with open(path, "w") as f:
        json.dump(hz, f)


def load_hotzones(path: str = "data/hotzones_seed.json") -> dict:
    return {"basis": "illustrative_seed", "cell_deg": 1.0, "note": "ILLUSTRATIVE stub seed, not measured",
            "cells": [{"lat": 57.5, "lon": 24.5, "n_anomalies": 12, "n_aircraft": 6, "max_severity": 0.8},
                      {"lat": 34.5, "lon": 33.5, "n_anomalies": 20, "n_aircraft": 9, "max_severity": 0.9}]}


def route_risk(origin: str, destination: str, hour_utc: int, hz: dict,
               corridor_km: float = 100.0) -> RouteRisk:
    pts = route_points(origin, destination)
    half = hz.get("cell_deg", 1.0) * 111 / 2
    cells = [RegionCell(**c) for c in hz.get("cells", [])
             if min(haversine_km(p, (c["lat"], c["lon"])) for p in pts) <= corridor_km + half]
    score = max((c.max_severity * min(1.0, c.n_anomalies / 10) for c in cells), default=0.0)
    advice = ["No reported interference along the route corridor."] if not cells else [
        "Brief crew on GNSS interference along the route; expect possible false terrain/position alerts.",
        "Cross-check position with DME/VOR/IRS; do not rely on GNSS alone in flagged cells.",
        "Check current NOTAMs for GNSS interference before departure.",
    ]
    basis = "observed" if hz.get("basis") == "observed" else "illustrative_seed"
    return RouteRisk(origin=origin, destination=destination, hour_utc=hour_utc, score=round(score, 2),
                     cells=cells, advice=advice, basis=basis)
