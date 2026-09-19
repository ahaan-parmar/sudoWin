"""SIMULATED GNSS interference, injected into a snapshot for demos and tests.

Nothing here touches a radio. It edits saved ADS-B observations so we can show
what spoofing and jamming look like in the data, and so detection has ground
truth to be scored against. Every snapshot produced here is kind="synthetic"
and carries an `injected` record saying exactly what was changed.

Model of a spoofer: it overrides what the receiver believes about its GNSS
position. The aircraft itself keeps flying normally, so barometric altitude and
the reported velocity and track stay true while the broadcast position moves.
That mismatch is what the detector is meant to find.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from .data import _destination, _haversine_km
from .models import InjectedEvent, Snapshot, StateVector

SPOOF_MODES = ("teleport", "drift", "attractor")


def _affected(s: Snapshot, source: tuple[float, float], radius_km: float,
              t_start: int, t_end: int, airborne_only: bool = False) -> set[int]:
    """Indexes of states inside the interference radius during the window.

    Distance is measured from the position as reported in the input snapshot.
    For a clean baseline that is the true position, which is what a real
    transmitter's footprint would depend on.
    """
    hit: set[int] = set()
    for i, st in enumerate(s.states):
        if not (t_start <= st.time <= t_end):
            continue
        if airborne_only and st.on_ground:
            continue
        if _haversine_km((st.lat, st.lon), source) <= radius_km:
            hit.add(i)
    return hit


def _capture_times(s: Snapshot, idx: set[int]) -> dict[str, int]:
    """First moment each aircraft came under the effect, for the ramped modes."""
    first: dict[str, int] = {}
    for i in idx:
        st = s.states[i]
        if st.icao24 not in first or st.time < first[st.icao24]:
            first[st.icao24] = st.time
    return first


def inject_spoof(s: Snapshot, source: tuple[float, float], radius_km: float,
                 t_start: int, t_end: int, mode: str = "teleport",
                 target: tuple[float, float] | None = None, seed: int = 0,
                 name: str | None = None) -> Snapshot:
    """Return a new snapshot with SIMULATED spoofing applied. Input untouched.

    teleport:  a fixed position offset appears while the aircraft is in range
    drift:     the offset grows from zero over the window
    attractor: reported positions are pulled toward `target` (default: source)
    """
    if mode not in SPOOF_MODES:
        raise ValueError(f"mode must be one of {SPOOF_MODES}, got {mode!r}")
    if radius_km <= 0:
        raise ValueError("radius_km must be positive")
    if t_end < t_start:
        raise ValueError("t_end must not be before t_start")

    rng = np.random.default_rng(seed)
    idx = _affected(s, source, radius_km, t_start, t_end)
    icaos = sorted({s.states[i].icao24 for i in idx})
    capture = _capture_times(s, idx)
    tgt = target if target is not None else source

    # draw per-aircraft parameters in sorted order so the result is reproducible
    params: dict[str, dict[str, float]] = {}
    for ic in icaos:
        params[ic] = {
            "bearing": float(rng.uniform(0.0, 360.0)),
            "teleport_km": float(rng.uniform(20.0, 60.0)),
            "drift_km": float(rng.uniform(30.0, 80.0)),
            "pull": float(rng.uniform(0.75, 0.95)),
            # A spoofed fix drags GNSS altitude with it. Barometric altitude
            # comes from a pressure sensor and stays put. Real baro and GNSS
            # altitude already sit several hundred metres apart (pressure
            # altitude is referenced to 1013.25 hPa, not to the ground), so the
            # injected offset has to clear that to be unambiguous.
            "geo_off_m": float(rng.uniform(600.0, 1500.0) * rng.choice([-1.0, 1.0])),
        }

    states: list[StateVector] = []
    for i, st in enumerate(s.states):
        new = st.model_copy(deep=True)
        if i in idx:
            p = params[st.icao24]
            frac = _ramp(st.time, capture[st.icao24], t_end) if mode != "teleport" else 1.0
            if mode == "teleport":
                new.lat, new.lon = _destination((st.lat, st.lon), p["bearing"], p["teleport_km"])
            elif mode == "drift":
                new.lat, new.lon = _destination((st.lat, st.lon), p["bearing"], p["drift_km"] * frac)
            else:  # attractor
                a = p["pull"] * frac
                new.lat = st.lat + a * (tgt[0] - st.lat)
                new.lon = st.lon + a * (tgt[1] - st.lon)
            new.lat = round(new.lat, 6)
            new.lon = round(new.lon, 6)
            if new.geo_alt_m is not None:
                new.geo_alt_m = round(new.geo_alt_m + p["geo_off_m"] * frac, 1)
            # baro_alt_m, velocity_ms and track_deg keep their true values:
            # the aircraft is still flying the same path it always was
        states.append(new)

    states.sort(key=lambda x: (x.icao24, x.time))
    event = InjectedEvent(
        mode=mode, source_lat=source[0], source_lon=source[1], radius_km=radius_km,
        t_start=t_start, t_end=t_end, affected_icao24=icaos,
        target_lat=tgt[0] if mode == "attractor" else None,
        target_lon=tgt[1] if mode == "attractor" else None,
    )
    return Snapshot(name=name or f"{s.name}_{mode}", bbox=s.bbox,
                    t_start=s.t_start, t_end=s.t_end, states=states,
                    kind="synthetic", injected=event)


def inject_jam(s: Snapshot, source: tuple[float, float], radius_km: float,
               t_start: int, t_end: int, seed: int = 0,
               name: str | None = None) -> Snapshot:
    """Return a new snapshot with SIMULATED jamming applied. Input untouched.

    Jamming denies the position fix, so affected aircraft stop reporting while
    they are in range. Neighbours outside the radius keep reporting, and that
    contrast is the signal.
    """
    if radius_km <= 0:
        raise ValueError("radius_km must be positive")
    if t_end < t_start:
        raise ValueError("t_end must not be before t_start")

    idx = _affected(s, source, radius_km, t_start, t_end, airborne_only=True)
    icaos = sorted({s.states[i].icao24 for i in idx})
    states = [st.model_copy(deep=True) for i, st in enumerate(s.states) if i not in idx]
    states.sort(key=lambda x: (x.icao24, x.time))

    event = InjectedEvent(
        mode="dropout", source_lat=source[0], source_lon=source[1],
        radius_km=radius_km, t_start=t_start, t_end=t_end, affected_icao24=icaos,
    )
    return Snapshot(name=name or f"{s.name}_jam", bbox=s.bbox,
                    t_start=s.t_start, t_end=s.t_end, states=states,
                    kind="synthetic", injected=event)


def _ramp(t: int, capture_t: int, t_end: int) -> float:
    """0 at the moment the aircraft was captured, 1 at the end of the window."""
    span = t_end - capture_t
    if span <= 0:
        return 1.0
    return min(1.0, max(0.0, (t - capture_t) / span))
