"""Shared data models for GNSS Watch. FROZEN: do not edit.

Conventions everywhere: (lat, lon) order, degrees; altitude in metres;
time = unix seconds UTC; distances in km; speeds in m/s.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Bbox = tuple[float, float, float, float]  # (lat_min, lon_min, lat_max, lon_max)


class StateVector(BaseModel):
    """One ADS-B observation of one aircraft at one time."""

    icao24: str
    callsign: Optional[str] = None
    time: int
    lat: float
    lon: float
    baro_alt_m: Optional[float] = None       # barometric altitude (not GNSS-derived)
    geo_alt_m: Optional[float] = None        # GNSS-derived altitude
    velocity_ms: Optional[float] = None      # reported ground speed
    track_deg: Optional[float] = None        # reported true track
    vertical_rate_ms: Optional[float] = None
    on_ground: bool = False
    nic: Optional[int] = None                # integrity fields: often unavailable, keep optional
    nacp: Optional[int] = None
    position_source: Optional[str] = None    # "adsb" | "mlat" | "other"


class InjectedEvent(BaseModel):
    """Ground truth for a synthetic event (used to score detection and localization)."""

    mode: Literal["teleport", "drift", "attractor", "dropout"]
    source_lat: float
    source_lon: float
    radius_km: float
    t_start: int
    t_end: int
    affected_icao24: list[str]
    target_lat: Optional[float] = None       # where spoofed positions are pulled to
    target_lon: Optional[float] = None


class Snapshot(BaseModel):
    """A time series of state vectors for a region. states sorted by (icao24, time)."""

    name: str
    bbox: Bbox
    t_start: int
    t_end: int
    states: list[StateVector]
    kind: Literal["real", "synthetic"] = "real"
    injected: Optional[InjectedEvent] = None  # only for synthetic snapshots with an event


AnomalyKind = Literal[
    "position_jump",
    "impossible_speed",
    "baro_geo_mismatch",
    "velocity_mismatch",
    "duplicate_id",
    "integrity_drop",
    "signal_dropout",
]


class Anomaly(BaseModel):
    """One suspicious observation.

    lat/lon semantics: for kind="position_jump" and "velocity_mismatch" caused by a jump,
    lat/lon is the last plausible position BEFORE the anomaly (best estimate of the true
    location); for every other kind it is the reported position at `time`.
    """

    icao24: str
    time: int
    lat: float
    lon: float
    alt_m: Optional[float] = None
    kind: AnomalyKind
    severity: float = Field(ge=0.0, le=1.0)
    detail: str


class SourceEstimate(BaseModel):
    lat: float
    lon: float
    radius_km: float                          # uncertainty radius
    confidence: float = Field(ge=0.0, le=1.0)
    n_anomalies: int
    method: str


class RegionCell(BaseModel):
    lat: float                                # cell centre
    lon: float
    n_anomalies: int
    n_aircraft: int
    max_severity: float = Field(ge=0.0, le=1.0)


class RouteRisk(BaseModel):
    origin: str
    destination: str
    hour_utc: int
    score: float = Field(ge=0.0, le=1.0)
    cells: list[RegionCell]                   # hot cells the route crosses
    advice: list[str]                         # short, factual, deterministic action items
    basis: Literal["illustrative_seed", "observed"] = "illustrative_seed"
