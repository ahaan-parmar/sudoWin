"""Data sources for GNSS Watch: a synthetic clean baseline, snapshot storage,
and a live OpenSky poller.

Conventions follow models.py: (lat, lon) in degrees, altitude in metres,
time in unix seconds UTC, distances in km, speeds in m/s.

The synthetic baseline is deliberately self-consistent. Reported velocity and
track are derived from the positions that were actually written, so a detector
cross-checking one against the other finds nothing. That is the point: the
baseline is the "clean" reference the simulated events are measured against.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from .models import Bbox, Snapshot, StateVector

# Repo root, so snapshots resolve the same way whether you run pytest from the
# root or the MCP server from somewhere else. Override with GNSSWATCH_DATA_DIR.
_REPO_ROOT = Path(__file__).resolve().parent.parent


def snapshot_dir() -> Path:
    """Directory holding saved snapshots."""
    env = os.environ.get("GNSSWATCH_DATA_DIR")
    if env:
        return Path(env)
    return _REPO_ROOT / "data" / "snapshots"


# Fixed reference time for synthetic data. Using the wall clock would make the
# baseline different on every run, which breaks seeded reproducibility and makes
# committed snapshot files churn. 2026-09-19 06:00:00 UTC.
BASE_TIME = 1789797600

EARTH_R_KM = 6371.0088
_M_PER_DEG_LAT = 111320.0

# Airline prefixes only, no tail numbers, so nothing here can be mistaken for a
# real flight.
_CALLSIGN_PREFIXES = (
    "AIC", "IGO", "SEJ", "UAE", "QTR", "THY", "DLH", "BAW", "AFR", "KLM",
    "SVA", "ETD", "OMA", "GFA", "MSR", "RJA", "ELY", "AZE", "UZB", "KZR",
)


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------

def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km between two (lat, lon) points."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(math.sqrt(min(1.0, h)))


def _bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Initial true bearing in degrees from a to b, 0..360."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _destination(origin: tuple[float, float], bearing_deg: float, dist_km: float) -> tuple[float, float]:
    """Point reached by travelling dist_km from origin along a great circle."""
    lat1, lon1 = math.radians(origin[0]), math.radians(origin[1])
    brg = math.radians(bearing_deg)
    ang = dist_km / EARTH_R_KM
    lat2 = math.asin(math.sin(lat1) * math.cos(ang) + math.cos(lat1) * math.sin(ang) * math.cos(brg))
    lon2 = lon1 + math.atan2(
        math.sin(brg) * math.sin(ang) * math.cos(lat1),
        math.cos(ang) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), (math.degrees(lon2) + 540.0) % 360.0 - 180.0


def _in_bbox(p: tuple[float, float], bbox: Bbox) -> bool:
    lat_min, lon_min, lat_max, lon_max = bbox
    return lat_min <= p[0] <= lat_max and lon_min <= p[1] <= lon_max


def _m_to_deg(dnorth_m: float, deast_m: float, lat: float) -> tuple[float, float]:
    """Small metre offsets to (dlat, dlon) degrees at the given latitude."""
    dlat = dnorth_m / _M_PER_DEG_LAT
    dlon = deast_m / (_M_PER_DEG_LAT * max(0.1, math.cos(math.radians(lat))))
    return dlat, dlon


# ---------------------------------------------------------------------------
# synthetic clean baseline
# ---------------------------------------------------------------------------

def _pick_leg(rng: np.random.Generator, bbox: Bbox, dist_km: float) -> tuple[tuple[float, float], float]:
    """Choose a start point and bearing whose great-circle leg stays in the bbox.

    Falls back to a centre-out leg if the bbox is smaller than the leg length;
    the aircraft then flies out of the region, which is normal and still clean.
    """
    lat_min, lon_min, lat_max, lon_max = bbox
    for _ in range(400):
        start = (rng.uniform(lat_min, lat_max), rng.uniform(lon_min, lon_max))
        brg = rng.uniform(0.0, 360.0)
        mid = _destination(start, brg, dist_km / 2.0)
        end = _destination(start, brg, dist_km)
        if _in_bbox(mid, bbox) and _in_bbox(end, bbox):
            return start, brg
    centre = ((lat_min + lat_max) / 2.0, (lon_min + lon_max) / 2.0)
    back = _destination(centre, rng.uniform(0.0, 360.0), dist_km / 2.0)
    return back, _bearing_deg(back, centre)


def _unique_icao24(rng: np.random.Generator, n: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    while len(out) < n:
        code = format(int(rng.integers(0x100000, 0xFFFFFF)), "06x")
        if code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def synthetic_baseline(bbox: Bbox, n_aircraft: int = 40, duration_s: int = 1800,
                       step_s: int = 10, seed: int = 0, name: str = "baseline") -> Snapshot:
    """Build a clean synthetic snapshot: level great-circle cruise, no anomalies.

    Every reported field is derived from the positions that end up in the file,
    so reported speed and track agree with the speed and track implied by the
    positions, and barometric and GNSS altitude stay a realistic few tens of
    metres apart. Same seed, same bytes.
    """
    if n_aircraft <= 0:
        raise ValueError("n_aircraft must be positive")
    if step_s <= 0:
        raise ValueError("step_s must be positive")
    if duration_s < step_s:
        raise ValueError("duration_s must be at least step_s")

    rng = np.random.default_rng(seed)
    t_start = BASE_TIME
    n_steps = int(duration_s // step_s) + 1
    times = [t_start + i * step_s for i in range(n_steps)]
    t_end = times[-1]

    icaos = _unique_icao24(rng, n_aircraft)
    states: list[StateVector] = []

    for icao in icaos:
        speed_ms = float(rng.uniform(200.0, 250.0))
        leg_km = speed_ms * duration_s / 1000.0
        start, brg = _pick_leg(rng, bbox, leg_km)

        # cruise flight levels, roughly 1000 ft apart
        baro_base = float(round(rng.uniform(9000.0, 12000.0) / 300.0) * 300.0)
        # GNSS altitude sits tens of metres off barometric altitude
        geo_offset = float(rng.uniform(15.0, 75.0) * rng.choice([-1.0, 1.0]))
        callsign = f"{_CALLSIGN_PREFIXES[int(rng.integers(0, len(_CALLSIGN_PREFIXES)))]}{int(rng.integers(100, 9999))}"

        # true track, then a small independent position error per sample
        pts: list[tuple[float, float]] = []
        for i in range(n_steps):
            true_p = _destination(start, brg, speed_ms * (i * step_s) / 1000.0)
            dn, de = rng.normal(0.0, 8.0, size=2)
            dlat, dlon = _m_to_deg(float(dn), float(de), true_p[0])
            # round here, not at write time, so the derived speed and track
            # match the coordinates that actually land in the file
            pts.append((round(true_p[0] + dlat, 6), round(true_p[1] + dlon, 6)))

        # velocity and track come from the points above, not from the ideal
        # track, so the two can never disagree
        seg_speed: list[float] = []
        seg_track: list[float] = []
        for i in range(n_steps - 1):
            seg_speed.append(_haversine_km(pts[i], pts[i + 1]) * 1000.0 / step_s)
            seg_track.append(_bearing_deg(pts[i], pts[i + 1]))
        seg_speed.append(seg_speed[-1])
        seg_track.append(seg_track[-1])

        baro_noise = rng.normal(0.0, 1.5, size=n_steps)
        geo_noise = rng.normal(0.0, 2.0, size=n_steps)

        for i, t in enumerate(times):
            baro = baro_base + float(baro_noise[i])
            states.append(StateVector(
                icao24=icao,
                callsign=callsign,
                time=t,
                lat=pts[i][0],
                lon=pts[i][1],
                baro_alt_m=round(baro, 1),
                geo_alt_m=round(baro + geo_offset + float(geo_noise[i]), 1),
                velocity_ms=round(seg_speed[i], 2),
                track_deg=round(seg_track[i], 2),
                vertical_rate_ms=0.0,
                on_ground=False,
                nic=8,
                nacp=9,
                position_source="adsb",
            ))

    states.sort(key=lambda s: (s.icao24, s.time))
    return Snapshot(name=name, bbox=bbox, t_start=t_start, t_end=t_end,
                    states=states, kind="synthetic", injected=None)


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def save_snapshot(s: Snapshot, path: str | None = None) -> str:
    """Write a snapshot as JSON. Defaults to data/snapshots/{name}.json."""
    target = Path(path) if path else snapshot_dir() / f"{s.name}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    # compact separators: these files get committed, and a 40-aircraft baseline
    # is about 7000 states
    target.write_text(json.dumps(s.model_dump(mode="json"), separators=(",", ":")),
                      encoding="utf-8")
    return str(target)


def load_snapshot(name_or_path: str) -> Snapshot:
    """Load a snapshot by bare name, by name.json, or by an explicit path."""
    for cand in _candidate_paths(name_or_path):
        if cand.is_file():
            return Snapshot.model_validate_json(cand.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"no snapshot named {name_or_path!r} in {snapshot_dir()}")


def _candidate_paths(name_or_path: str) -> Iterable[Path]:
    p = Path(name_or_path)
    yield p
    if p.suffix != ".json":
        yield p.with_suffix(".json")
    if not p.is_absolute() and len(p.parts) == 1:
        stem = p.stem if p.suffix == ".json" else p.name
        yield snapshot_dir() / f"{stem}.json"


def list_snapshots() -> list[str]:
    """Names of saved snapshots, sorted. Empty if the directory is missing."""
    d = snapshot_dir()
    if not d.is_dir():
        return []
    return sorted(f.stem for f in d.glob("*.json") if f.is_file())


# ---------------------------------------------------------------------------
# live OpenSky feed
# ---------------------------------------------------------------------------
#
# OpenSky now accepts only the OAuth2 client credentials flow; username and
# password basic auth is gone. Put an API client in the environment:
#
#     OPENSKY_CLIENT_ID=...
#     OPENSKY_CLIENT_SECRET=...
#
# Without them the anonymous tier still answers /states/all, on a much smaller
# daily credit allowance. Either way this is read-only public data.
#
# The REST feed gives one position per aircraft per request and no history, so
# short tracks have to be built by polling. Credits are limited, so keep
# `polls` modest and save what you get.

OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network"
    "/protocol/openid-connect/token"
)
OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"

# index -> meaning, from the OpenSky REST docs
_POSITION_SOURCE = {0: "adsb", 1: "other", 2: "mlat", 3: "other"}

_token_cache: dict[str, float | str] = {}


def _opensky_token(client: "httpx.Client") -> Optional[str]:
    """Fetch and cache an access token. None means fall back to anonymous."""
    import time as _time

    cid = os.environ.get("OPENSKY_CLIENT_ID")
    secret = os.environ.get("OPENSKY_CLIENT_SECRET")
    if not cid or not secret:
        return None

    tok = _token_cache.get("token")
    exp = float(_token_cache.get("expires_at", 0.0))
    if tok and _time.time() < exp - 30:
        return str(tok)

    try:
        r = client.post(OPENSKY_TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": cid,
            "client_secret": secret,
        }, timeout=20.0)
        r.raise_for_status()
        body = r.json()
    except Exception as exc:  # noqa: BLE001 - a bad token must not kill the poll
        print(f"[gnsswatch] OpenSky token request failed ({exc}); continuing anonymously")
        return None

    token = body.get("access_token")
    if not token:
        return None
    _token_cache["token"] = token
    _token_cache["expires_at"] = _time.time() + float(body.get("expires_in", 1800))
    return str(token)


def _parse_states(rows: Iterable, seen: set[tuple[str, int]]) -> list[StateVector]:
    """Turn raw OpenSky rows into StateVectors, skipping unusable ones.

    NIC and NACp are not in this feed, so they stay None and detectors have to
    work without them.
    """
    out: list[StateVector] = []
    for row in rows or []:
        try:
            icao24 = (row[0] or "").strip().lower()
            lon, lat = row[5], row[6]
            t = row[3] if row[3] is not None else row[4]
            if not icao24 or lat is None or lon is None or t is None:
                continue
            key = (icao24, int(t))
            if key in seen:
                continue
            seen.add(key)
            callsign = (row[1] or "").strip() or None
            out.append(StateVector(
                icao24=icao24,
                callsign=callsign,
                time=int(t),
                lat=float(lat),
                lon=float(lon),
                baro_alt_m=_as_float(row[7]),
                geo_alt_m=_as_float(row[13]),
                velocity_ms=_as_float(row[9]),
                track_deg=_as_float(row[10]),
                vertical_rate_ms=_as_float(row[11]),
                on_ground=bool(row[8]),
                position_source=_POSITION_SOURCE.get(row[16] if len(row) > 16 else None, "other"),
            ))
        except (IndexError, TypeError, ValueError):
            continue
    return out


def _as_float(v) -> Optional[float]:
    return None if v is None else float(v)


def fetch_live(bbox: Bbox, polls: int = 8, interval_s: int = 12, name: str = "live") -> Snapshot:
    """Poll OpenSky several times to build short tracks over a bounding box.

    Returns whatever it managed to collect. Network failures, auth failures and
    rate limits are logged and skipped, never raised, so a demo that loses its
    connection still produces a snapshot instead of a traceback.
    """
    import time as _time

    import httpx

    lat_min, lon_min, lat_max, lon_max = bbox
    params = {"lamin": lat_min, "lomin": lon_min, "lamax": lat_max, "lomax": lon_max}

    states: list[StateVector] = []
    seen: set[tuple[str, int]] = set()
    started = int(_time.time())
    backoff = float(interval_s)

    with httpx.Client(timeout=30.0) as client:
        token = _opensky_token(client)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if not token:
            print("[gnsswatch] no OpenSky credentials in env; using the anonymous tier")

        for i in range(max(1, polls)):
            if i:
                _time.sleep(interval_s)
            try:
                r = client.get(OPENSKY_STATES_URL, params=params, headers=headers)
            except Exception as exc:  # noqa: BLE001
                print(f"[gnsswatch] poll {i + 1}/{polls} failed ({exc}); skipping")
                continue

            if r.status_code in (429, 503):
                wait = _retry_after(r, backoff)
                print(f"[gnsswatch] rate limited on poll {i + 1}/{polls}; waiting {wait:.0f}s")
                _time.sleep(wait)
                backoff = min(backoff * 2, 120.0)
                continue
            if r.status_code in (401, 403):
                # token expired or credits exhausted; drop to anonymous and go on
                print(f"[gnsswatch] OpenSky returned {r.status_code}; dropping auth header")
                headers = {}
                continue
            if r.status_code != 200:
                print(f"[gnsswatch] OpenSky returned {r.status_code} on poll {i + 1}/{polls}")
                continue

            try:
                body = r.json()
            except ValueError:
                print(f"[gnsswatch] poll {i + 1}/{polls} returned non-JSON; skipping")
                continue
            states.extend(_parse_states(body.get("states"), seen))

    states.sort(key=lambda s: (s.icao24, s.time))
    if states:
        t_start, t_end = states[0].time, states[0].time
        for s in states:
            t_start = min(t_start, s.time)
            t_end = max(t_end, s.time)
    else:
        print("[gnsswatch] no live states collected; returning an empty snapshot")
        t_start = started
        t_end = int(_time.time())
    return Snapshot(name=name, bbox=bbox, t_start=t_start, t_end=t_end,
                    states=states, kind="real", injected=None)


def _retry_after(resp, default_s: float) -> float:
    try:
        return max(1.0, min(120.0, float(resp.headers.get("Retry-After", default_s))))
    except (TypeError, ValueError):
        return default_s
