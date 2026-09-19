"""Hot zones and route risk. (P3)

A "hot zone" is a lat/lon cell, optionally tied to an hour of the day UTC, in
which GNSS anomalies have been seen. `build_hotzones` aggregates a history of
anomalies into those cells; `route_risk` scores a great-circle route against
them so a flight-ops team can be warned BEFORE departure rather than told
afterwards.

Hot-zone file schema (JSON, version 1)
--------------------------------------
{
  "version": 1,
  "cell_deg": 1.0,
  "basis": "observed" | "illustrative_seed",
  "label": "<free text; says plainly what the data is>",
  "n_anomalies": <int>, "n_aircraft": <int>,
  "cells": [
    {"lat": <cell centre>, "lon": <cell centre>,
     "hour_utc": <0-23, or null meaning "all hours">,
     "n_anomalies": <int>, "n_aircraft": <int>,
     "max_severity": <0-1>, "score": <0-1>}
  ]
}

`basis` propagates to RouteRisk.basis. It is "observed" only for zones built
from real detector output by `build_hotzones`. The shipped seed file is
"illustrative_seed": illustrative, NOT measured, and must never be presented
as a measurement.
"""
from __future__ import annotations

import json
import math
import os
from typing import Any

from . import airports
from .locate import haversine_km
from .models import Anomaly, RegionCell, RouteRisk

SCHEMA_VERSION = 1
SEED_PATH = "data/hotzones_seed.json"

# route_risk resamples the route at roughly this spacing so a hot cell cannot
# slip between two sample points.
_ROUTE_SAMPLE_KM = 25.0
_ROUTE_MIN_POINTS = 50
_ROUTE_MAX_POINTS = 400

# Cell score weights. A cell is riskier when more distinct aircraft were
# affected there (saturating at 5, since a handful already establishes it) and
# when the worst anomaly there was severe.
_W_AIRCRAFT = 0.6
_W_SEVERITY = 0.4
_AIRCRAFT_SATURATION = 5.0


def _cell_centre(value: float, cell_deg: float) -> float:
    """Centre of the cell containing `value`, rounded to kill float fuzz."""
    return round(math.floor(value / cell_deg) * cell_deg + cell_deg / 2.0, 6)


def _cell_score(n_aircraft: int, max_severity: float) -> float:
    density = min(1.0, n_aircraft / _AIRCRAFT_SATURATION)
    return round(max(0.0, min(1.0, _W_AIRCRAFT * density + _W_SEVERITY * max_severity)), 4)


def build_hotzones(history: list[Anomaly], cell_deg: float = 1.0) -> dict:
    """Aggregate a history of anomalies into hot cells by (cell, hour UTC).

    The result is JSON-serialisable and carries basis="observed", since it is
    built from detector output rather than from the illustrative seed.
    """
    if cell_deg <= 0:
        raise ValueError(f"cell_deg must be positive, got {cell_deg}")

    buckets: dict[tuple[float, float, int], dict[str, Any]] = {}
    all_aircraft: set[str] = set()
    for a in history:
        hour = int((a.time // 3600) % 24)
        key = (_cell_centre(a.lat, cell_deg), _cell_centre(a.lon, cell_deg), hour)
        b = buckets.setdefault(key, {"n_anomalies": 0, "aircraft": set(), "max_severity": 0.0})
        b["n_anomalies"] += 1
        b["aircraft"].add(a.icao24)
        b["max_severity"] = max(b["max_severity"], a.severity)
        all_aircraft.add(a.icao24)

    cells = []
    for (lat, lon, hour), b in buckets.items():
        n_ac = len(b["aircraft"])
        cells.append({
            "lat": lat,
            "lon": lon,
            "hour_utc": hour,
            "n_anomalies": b["n_anomalies"],
            "n_aircraft": n_ac,
            "max_severity": round(b["max_severity"], 4),
            "score": _cell_score(n_ac, b["max_severity"]),
        })
    # Deterministic order: strongest first, then geography, then hour.
    cells.sort(key=lambda c: (-c["score"], c["lat"], c["lon"], c["hour_utc"]))

    return {
        "version": SCHEMA_VERSION,
        "cell_deg": cell_deg,
        "basis": "observed",
        "label": "Built from detected anomalies in this session.",
        "n_anomalies": len(history),
        "n_aircraft": len(all_aircraft),
        "cells": cells,
    }


def save_hotzones(hz: dict, path: str = "data/hotzones.json") -> None:
    """Write hot zones to `path` as JSON, creating parent directories."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(hz, fh, indent=2, sort_keys=True)
        fh.write("\n")


def load_hotzones(path: str = SEED_PATH) -> dict:
    """Read hot zones from `path`.

    Raises FileNotFoundError if the file is missing, rather than returning an
    empty set of zones: silently scoring every route as safe because a data
    file was absent is the wrong failure mode for a warning tool.
    """
    with open(path, encoding="utf-8") as fh:
        hz = json.load(fh)
    if not isinstance(hz, dict) or "cells" not in hz:
        raise ValueError(f"{path} is not a hot-zone file (no 'cells' key)")
    return hz


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

def _to_vec(lat: float, lon: float) -> tuple[float, float, float]:
    rlat, rlon = math.radians(lat), math.radians(lon)
    c = math.cos(rlat)
    return (c * math.cos(rlon), c * math.sin(rlon), math.sin(rlat))


def _to_latlon(v: tuple[float, float, float]) -> tuple[float, float]:
    x, y, z = v
    norm = math.sqrt(x * x + y * y + z * z)
    x, y, z = x / norm, y / norm, z / norm
    return (round(math.degrees(math.asin(max(-1.0, min(1.0, z)))), 6),
            round(math.degrees(math.atan2(y, x)), 6))


def route_points(origin: str, destination: str, n: int = 50) -> list[tuple[float, float]]:
    """`n` points along the great circle from origin to destination, inclusive
    of both endpoints.

    Endpoints are ICAO codes from `airports.AIRPORTS` or "lat,lon" strings.
    Interpolation is spherical (slerp), so the points follow the real flown
    track rather than a straight line in lat/lon, which matters a great deal
    on the high-latitude Baltic routes this project cares about.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    a = airports.resolve(origin)
    b = airports.resolve(destination)
    if n == 1:
        return [a]

    v1, v2 = _to_vec(*a), _to_vec(*b)
    dot = max(-1.0, min(1.0, sum(p * q for p, q in zip(v1, v2))))
    ang = math.acos(dot)

    if ang < 1e-9:                                    # same point
        return [a] * n
    if abs(ang - math.pi) < 1e-9:                     # antipodal: no unique path
        raise ValueError(
            f"{origin} and {destination} are antipodal; the great circle between "
            "them is not unique"
        )

    sin_ang = math.sin(ang)
    out = []
    for i in range(n):
        t = i / (n - 1)
        s1 = math.sin((1.0 - t) * ang) / sin_ang
        s2 = math.sin(t * ang) / sin_ang
        out.append(_to_latlon(tuple(s1 * p + s2 * q for p, q in zip(v1, v2))))
    return out


def _advice(score: float, cells: list[RegionCell], basis: str,
            hour_utc: int, corridor_km: float) -> list[str]:
    """Short, factual, deterministic action items. Same inputs, same output.

    Deliberately no hedging language and no severity adjectives beyond the
    band: the assistant reading this should relay it, not embellish it.
    """
    out: list[str] = []
    if not cells:
        out.append(
            f"No hot cells within {corridor_km:.0f} km of this route at {hour_utc:02d}:00Z "
            "in the loaded hot-zone data."
        )
        out.append("No GNSS-specific action required beyond normal procedures.")
    else:
        worst = max(c.max_severity for c in cells)
        n_ac = sum(c.n_aircraft for c in cells)
        out.append(
            f"{len(cells)} hot cell(s) within {corridor_km:.0f} km of the route at "
            f"{hour_utc:02d}:00Z; {n_ac} aircraft affected in that data, "
            f"worst severity {worst:.2f}."
        )
        out.append("Expect GNSS degradation en route; treat GNSS position as unverified in those cells.")
        out.append("Brief the crew before departure on the affected segment and its timing.")
        out.append("Confirm a non-GNSS approach (ILS/VOR/DME) is available and current at destination and alternate.")
        if score >= 0.5:
            out.append("Confirm conventional navaid coverage along the affected segment before dispatch.")
            out.append("Carry fuel for a non-GNSS approach or a diversion to an alternate with one.")
        if score >= 0.75:
            out.append("Consider a routing that avoids the affected cells, or a departure at a different hour.")
        out.append("Report any interference to ATC and file the operator's GNSS interference report after landing.")

    if basis == "illustrative_seed":
        out.append(
            "BASIS: ILLUSTRATIVE SEED DATA, NOT MEASURED. Indicative of publicly "
            "reported regions only. Not for operational planning."
        )
    else:
        out.append(
            "BASIS: observed anomalies from public ADS-B in this session. "
            "Situational awareness only, not a certified operational tool."
        )
    return out


def route_risk(origin: str, destination: str, hour_utc: int, hz: dict,
               corridor_km: float = 100.0) -> RouteRisk:
    """Score a route against hot zones at a given hour UTC.

    A cell counts when its centre lies within `corridor_km` of the great-circle
    track. Cells with hour_utc null apply at every hour; otherwise the hour
    must match.

    The route score combines cells as independent contributions:

        score = 1 - PROD(1 - w_i * s_i)

    where s_i is the cell score and w_i falls linearly from 1.0 on track to 0.5
    at the corridor edge. So more hot cells raise the score, a cell squarely on
    the route counts double one at the edge, and the score saturates towards 1
    instead of being pinned by a single worst cell.
    """
    if not 0 <= hour_utc <= 23:
        raise ValueError(f"hour_utc must be 0-23, got {hour_utc}")
    if corridor_km <= 0:
        raise ValueError(f"corridor_km must be positive, got {corridor_km}")

    # Never claim "observed" unless the file says so: unknown basis is treated
    # as illustrative, which is the conservative direction for a warning.
    basis = hz.get("basis")
    if basis not in ("observed", "illustrative_seed"):
        basis = "illustrative_seed"

    dist_km = haversine_km(airports.resolve(origin), airports.resolve(destination))
    n_pts = int(min(_ROUTE_MAX_POINTS,
                    max(_ROUTE_MIN_POINTS, dist_km / _ROUTE_SAMPLE_KM + 2)))
    track = route_points(origin, destination, n=n_pts)

    hits: list[tuple[float, float, dict]] = []   # (score_contribution, distance, cell)
    for cell in hz.get("cells", []):
        cell_hour = cell.get("hour_utc")
        if cell_hour is not None and int(cell_hour) != hour_utc:
            continue
        centre = (float(cell["lat"]), float(cell["lon"]))
        d = min(haversine_km(centre, p) for p in track)
        if d > corridor_km:
            continue
        weight = 1.0 - 0.5 * (d / corridor_km)
        hits.append((weight * float(cell.get("score", 0.0)), d, cell))

    survival = 1.0
    for contribution, _, _ in hits:
        survival *= (1.0 - max(0.0, min(1.0, contribution)))
    score = max(0.0, min(1.0, 1.0 - survival))

    # Strongest contribution first, then by proximity, for a stable brief.
    hits.sort(key=lambda h: (-h[0], h[1], h[2]["lat"], h[2]["lon"]))
    cells = [
        RegionCell(
            lat=float(c["lat"]),
            lon=float(c["lon"]),
            n_anomalies=int(c.get("n_anomalies", 0)),
            n_aircraft=int(c.get("n_aircraft", 0)),
            max_severity=max(0.0, min(1.0, float(c.get("max_severity", 0.0)))),
        )
        for _, _, c in hits
    ]

    return RouteRisk(
        origin=origin,
        destination=destination,
        hour_utc=hour_utc,
        score=round(score, 4),
        cells=cells,
        advice=_advice(score, cells, basis, hour_utc, corridor_km),
        basis=basis,
    )
