"""Estimate where GNSS interference is coming from. (P3)

Method
------
A ground transmitter can only affect an aircraft that is within radio line of
sight (LoS) of it. For a transmitter at height `h_tx_m` and an aircraft at
`h_ac_m`, the LoS horizon is approximately

    d_km = 3.57 * (sqrt(h_tx_m) + sqrt(h_ac_m))

the standard 4/3-Earth-radius VHF/UHF horizon approximation. We assume a
ground-based transmitter at H_TX_M = 10 m, a fixed ~11.3 km contribution.

Each affected aircraft therefore constrains the source to a disc centred on
that aircraft, of radius d_km. The source lies in the INTERSECTION of those
discs. `estimate_source` finds it by grid search: score every candidate point
by how many distinct aircraft could see it, keep the best-scoring region, and
return its centroid with the region's spread as the uncertainty radius.

Positions used are `Anomaly.lat/lon`. Per models.py that is the last plausible
position BEFORE the anomaly for position jumps, i.e. where the aircraft really
was when it was affected -- not the spoofed position. That is exactly what this
method needs, so no special-casing by anomaly kind is required here.

Assumptions and limits (stated plainly; this is situational awareness, not a fix)
--------------------------------------------------------------------------------
  * Geometry only. There is no power, antenna-pattern, terrain or refraction
    model, so the discs bound where a transmitter *could* be seen from, not
    where one of a given strength actually reaches. A strong transmitter can
    affect aircraft past the geometric horizon; a weak one may not reach it.
  * The discs are large. At 10 km altitude the LoS radius is ~368 km, so a
    tight fix needs aircraft spread AROUND the source. Aircraft clustered in
    one direction give a large, poorly constrained region, and `confidence`
    is reduced accordingly.
  * A single transmitter is assumed. Two separate sources produce anomalies
    with no common intersection; that shows up as a partial intersection and
    a low confidence, not as an error.
  * Anomalies are assumed to share a cause. We do not check that they are
    close in time; the caller should pass one event's anomalies.
  * Aircraft altitude falls back to DEFAULT_AC_ALT_M when the anomaly carries
    no altitude, which is the common case for the ADS-B feeds we use.
  * The result is a plausible source REGION, not a bearing-based fix, and must
    not be presented as one.

`method` on the returned SourceEstimate says which branch produced it:
  "los_intersection"          every aircraft can see the returned region
  "los_partial_intersection"  the best region is seen by k of n aircraft (k<n)
  "weighted_centroid"         no usable overlap; severity-weighted mean instead
"""
from __future__ import annotations

import math

from .models import Anomaly, SourceEstimate

EARTH_R_KM = 6371.0088          # IUGG mean radius
H_TX_M = 10.0                   # assumed ground transmitter height, metres
DEFAULT_AC_ALT_M = 10000.0      # assumed cruise altitude when none reported
MIN_AIRCRAFT = 3                # below this the geometry is meaningless
_LOS_K = 3.57                   # km per sqrt(metre), 4/3-Earth-radius horizon
_TX_TERM = _LOS_K * math.sqrt(H_TX_M)

_GRID_STEPS = 40                # candidate points per axis, per pass
_REFINE_PASSES = 3              # coarse pass plus refinements


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km between (lat, lon) points in degrees."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2.0) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
    return 2.0 * EARTH_R_KM * math.asin(math.sqrt(min(1.0, h)))


def los_radius_km(alt_m: float | None) -> float:
    """Radio line-of-sight radius from a 10 m ground transmitter to `alt_m`."""
    h = DEFAULT_AC_ALT_M if alt_m is None else float(alt_m)
    if not math.isfinite(h) or h < 0.0:
        h = 0.0
    return _TX_TERM + _LOS_K * math.sqrt(h)


def bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Initial great-circle bearing from a to b, degrees clockwise from north."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.degrees(math.atan2(y, x)) % 360.0


# --------------------------------------------------------------------------
# longitude handling
#
# All search maths runs in a frame whose origin longitude is the circular mean
# of the input longitudes, with offsets in (-180, 180]. That makes a cluster
# spanning the antimeridian contiguous instead of splitting at +/-180.
# --------------------------------------------------------------------------

def _wrap180(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def _circular_mean_lon(lons: list[float]) -> float:
    x = sum(math.cos(math.radians(v)) for v in lons)
    y = sum(math.sin(math.radians(v)) for v in lons)
    if abs(x) < 1e-12 and abs(y) < 1e-12:
        return 0.0
    return math.degrees(math.atan2(y, x))


def _unit_vector_centroid(points: list[tuple[float, float]],
                          weights: list[float]) -> tuple[float, float]:
    """Weighted mean direction of (lat, lon) points, via 3-D unit vectors.

    Averaging on the sphere rather than in lat/lon avoids both the
    antimeridian discontinuity and the distortion of averaging longitudes at
    high latitude.
    """
    x = y = z = 0.0
    for (lat, lon), w in zip(points, weights):
        rlat, rlon = math.radians(lat), math.radians(lon)
        clat = math.cos(rlat)
        x += w * clat * math.cos(rlon)
        y += w * clat * math.sin(rlon)
        z += w * math.sin(rlat)
    norm = math.sqrt(x * x + y * y + z * z)
    if norm < 1e-12:                       # antipodal inputs cancel out
        return (points[0][0], points[0][1])
    return (math.degrees(math.asin(z / norm)), math.degrees(math.atan2(y, x)))


class _Aircraft:
    """One aircraft's LoS constraints: it must be able to see the source from
    every position at which it reported an anomaly."""

    __slots__ = ("icao24", "discs", "severity", "ref")

    def __init__(self, icao24: str, anoms: list[Anomaly]):
        self.icao24 = icao24
        # (lat, lon, radius_km) per anomaly, de-duplicated on position+radius
        seen: set[tuple[float, float, float]] = set()
        discs: list[tuple[float, float, float]] = []
        for a in anoms:
            key = (a.lat, a.lon, los_radius_km(a.alt_m))
            if key not in seen:
                seen.add(key)
                discs.append(key)
        self.discs = discs
        self.severity = max(a.severity for a in anoms)
        earliest = min(anoms, key=lambda a: a.time)
        self.ref = (earliest.lat, earliest.lon)   # representative position

    def sees(self, lat: float, lon: float) -> bool:
        for dlat, dlon, radius in self.discs:
            if haversine_km((lat, lon), (dlat, dlon)) > radius:
                return False
        return True


def _grid_search(aircraft: list[_Aircraft], lat_lo: float, lat_hi: float,
                 x_lo: float, x_hi: float, ref_lon: float,
                 steps: int) -> tuple[int, list[tuple[float, float]]]:
    """Score a lat/lon-offset grid by how many aircraft can see each point.

    Returns (best_count, points_at_best_count) with points as (lat, x), where
    x is a longitude offset from ref_lon.
    """
    lat_step = (lat_hi - lat_lo) / max(1, steps - 1)
    x_step = (x_hi - x_lo) / max(1, steps - 1)
    best = -1
    winners: list[tuple[float, float]] = []
    for i in range(steps):
        lat = lat_lo + i * lat_step
        if not -90.0 <= lat <= 90.0:
            continue
        for j in range(steps):
            x = x_lo + j * x_step
            lon = _wrap180(ref_lon + x)
            count = 0
            for ac in aircraft:
                if ac.sees(lat, lon):
                    count += 1
            if count > best:
                best = count
                winners = [(lat, x)]
            elif count == best:
                winners.append((lat, x))
    return best, winners


def _confidence(n_aircraft: int, n_seen: int, radius_km: float,
                centre: tuple[float, float], aircraft: list[_Aircraft],
                mean_los_km: float) -> float:
    """Blend of how much evidence there is, how good the geometry is, and how
    tight the resulting region is. Deterministic; weights documented here.

    c_count  more distinct aircraft is stronger evidence (saturates at 8)
    c_geom   aircraft spread around the estimate constrain it far better than
             aircraft clustered on one side. Measured as 1 - |mean resultant
             vector| of the bearings to them: 0 = all one direction, 1 = evenly
             surrounding. Same idea as GDOP.
    c_tight  a region much smaller than a single LoS disc is a real fix; one
             the size of a disc means the discs barely overlapped.
    """
    c_count = min(1.0, n_aircraft / 8.0)

    bearings = [math.radians(bearing_deg(centre, ac.ref)) for ac in aircraft]
    if bearings:
        bx = sum(math.cos(b) for b in bearings) / len(bearings)
        by = sum(math.sin(b) for b in bearings) / len(bearings)
        c_geom = 1.0 - min(1.0, math.hypot(bx, by))
    else:
        c_geom = 0.0

    c_tight = 1.0 - min(1.0, radius_km / mean_los_km) if mean_los_km > 0 else 0.0

    score = 0.40 * c_count + 0.35 * c_geom + 0.25 * c_tight
    # Penalise a region that not every aircraft can see: that means either an
    # outlier anomaly or more than one source.
    if n_aircraft > 0:
        score *= n_seen / n_aircraft
    return max(0.0, min(1.0, score))


def estimate_source(anoms: list[Anomaly]) -> SourceEstimate | None:
    """Estimate the likely interference source from a set of anomalies.

    Returns None for no anomalies, or for fewer than MIN_AIRCRAFT distinct
    aircraft: one or two aircraft cannot constrain a position, and returning a
    confident-looking point from them would be misleading.
    """
    if not anoms:
        return None

    by_ac: dict[str, list[Anomaly]] = {}
    for a in anoms:
        by_ac.setdefault(a.icao24, []).append(a)
    if len(by_ac) < MIN_AIRCRAFT:
        return None

    aircraft = [_Aircraft(icao, group) for icao, group in sorted(by_ac.items())]
    n_ac = len(aircraft)
    n_anoms = len(anoms)

    all_points = [(lat, lon) for ac in aircraft for lat, lon, _ in ac.discs]
    radii = [r for ac in aircraft for _, _, r in ac.discs]
    mean_los_km = sum(radii) / len(radii)
    max_radius_km = max(radii)

    ref_lon = _circular_mean_lon([lon for _, lon in all_points])
    lats = [lat for lat, _ in all_points]
    xs = [_wrap180(lon - ref_lon) for _, lon in all_points]

    # Start from the bounding box of the anomaly positions grown by the largest
    # LoS radius: the source cannot lie outside that and still have been seen.
    pad_lat = max_radius_km / 111.32
    mid_lat = (min(lats) + max(lats)) / 2.0
    pad_x = max_radius_km / (111.32 * max(0.15, math.cos(math.radians(mid_lat))))
    lat_lo, lat_hi = max(-90.0, min(lats) - pad_lat), min(90.0, max(lats) + pad_lat)
    x_lo, x_hi = min(xs) - pad_x, max(xs) + pad_x

    best = -1
    winners: list[tuple[float, float]] = []
    for _ in range(_REFINE_PASSES):
        best, winners = _grid_search(aircraft, lat_lo, lat_hi, x_lo, x_hi,
                                     ref_lon, _GRID_STEPS)
        if best <= 0 or not winners:
            break
        # Zoom onto the winning region, padded by one cell so the true optimum
        # is not clipped by the coarse grid.
        lat_step = (lat_hi - lat_lo) / (_GRID_STEPS - 1)
        x_step = (x_hi - x_lo) / (_GRID_STEPS - 1)
        w_lats = [p[0] for p in winners]
        w_xs = [p[1] for p in winners]
        lat_lo, lat_hi = min(w_lats) - lat_step, max(w_lats) + lat_step
        x_lo, x_hi = min(w_xs) - x_step, max(w_xs) + x_step

    if best <= 0 or not winners:
        return _fallback_centroid(aircraft, anoms, n_anoms, mean_los_km)

    pts = [(lat, _wrap180(ref_lon + x)) for lat, x in winners]
    centre = _unit_vector_centroid(pts, [1.0] * len(pts))

    # Uncertainty = how far the feasible region reaches from its centroid, plus
    # half a final grid cell for the discretisation itself.
    final_lat_step = (lat_hi - lat_lo) / (_GRID_STEPS - 1)
    cell_km = max(final_lat_step * 111.32, 0.1)
    spread_km = max((haversine_km(centre, p) for p in pts), default=0.0)
    radius_km = spread_km + cell_km / 2.0

    method = "los_intersection" if best >= n_ac else "los_partial_intersection"
    conf = _confidence(n_ac, best, radius_km, centre, aircraft, mean_los_km)

    return SourceEstimate(
        lat=round(centre[0], 5),
        lon=round(centre[1], 5),
        radius_km=round(radius_km, 2),
        confidence=round(conf, 3),
        n_anomalies=n_anoms,
        method=method,
    )


def _fallback_centroid(aircraft: list[_Aircraft], anoms: list[Anomaly],
                       n_anoms: int, mean_los_km: float) -> SourceEstimate:
    """No candidate point is visible to any aircraft (degenerate input): fall
    back to the severity-weighted mean position, with the spread as the radius
    and confidence capped low to say plainly this is not a geometric fix."""
    pts = [(a.lat, a.lon) for a in anoms]
    weights = [max(a.severity, 1e-3) for a in anoms]
    centre = _unit_vector_centroid(pts, weights)
    total = sum(weights)
    spread = math.sqrt(
        sum(w * haversine_km(centre, p) ** 2 for p, w in zip(pts, weights)) / total
    )
    conf = _confidence(len(aircraft), len(aircraft), spread, centre, aircraft, mean_los_km)
    return SourceEstimate(
        lat=round(centre[0], 5),
        lon=round(centre[1], 5),
        radius_km=round(max(spread, 1.0), 2),
        confidence=round(min(conf, 0.35), 3),
        n_anomalies=n_anoms,
        method="weighted_centroid",
    )
