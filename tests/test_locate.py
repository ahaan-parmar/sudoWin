"""Tests for gnsswatch.locate (P3).

Includes a tiny self-contained simulator: put a transmitter somewhere, scatter
aircraft around it, keep the ones within radio line of sight, and turn those
into anomalies (optionally with position noise). Localization error is then
measured against the known truth. Inline fixtures only -- nothing here depends
on another person's module.
"""
from __future__ import annotations

import math
import random

import pytest

from gnsswatch.locate import (
    MIN_AIRCRAFT,
    bearing_deg,
    estimate_source,
    haversine_km,
    los_radius_km,
)
from gnsswatch.models import Anomaly

EARTH_R_KM = 6371.0088


# --------------------------------------------------------------------------
# simulator
# --------------------------------------------------------------------------

def destination_point(lat: float, lon: float, d_km: float, brg_deg: float) -> tuple[float, float]:
    """Point `d_km` from (lat, lon) along bearing `brg_deg`."""
    ad = d_km / EARTH_R_KM
    b = math.radians(brg_deg)
    la = math.radians(lat)
    la2 = math.asin(math.sin(la) * math.cos(ad) + math.cos(la) * math.sin(ad) * math.cos(b))
    lo2 = math.radians(lon) + math.atan2(
        math.sin(b) * math.sin(ad) * math.cos(la),
        math.cos(ad) - math.sin(la) * math.sin(la2),
    )
    return (math.degrees(la2), (math.degrees(lo2) + 540.0) % 360.0 - 180.0)


def simulate_event(source: tuple[float, float], n_aircraft: int = 30,
                   alt_m: float = 10000.0, scatter_km: float = 900.0,
                   noise_km: float = 0.0, seed: int = 0,
                   bearings: tuple[float, float] = (0.0, 360.0),
                   t0: int = 1_700_000_000) -> list[Anomaly]:
    """Scatter aircraft around `source`, keep those in line of sight, return
    one anomaly each at the position where they were affected.

    `noise_km` models the detector's imperfect estimate of the last plausible
    position before the jump. `bearings` restricts which side of the source
    the aircraft sit on, to build deliberately bad geometry.
    """
    rng = random.Random(seed)
    los = los_radius_km(alt_m)
    out: list[Anomaly] = []
    for i in range(n_aircraft):
        d = rng.uniform(0.0, scatter_km)
        brg = rng.uniform(*bearings)
        lat, lon = destination_point(source[0], source[1], d, brg)
        if haversine_km((lat, lon), source) > los:
            continue                     # out of line of sight: not affected
        if noise_km > 0.0:
            lat, lon = destination_point(lat, lon, rng.uniform(0.0, noise_km),
                                         rng.uniform(0.0, 360.0))
        out.append(Anomaly(
            icao24=f"sim{i:03d}", time=t0 + i * 10, lat=lat, lon=lon, alt_m=alt_m,
            kind="position_jump", severity=0.8,
            detail="SIMULATED test fixture, not real traffic",
        ))
    return out


def localization_error_km(anoms: list[Anomaly], truth: tuple[float, float]) -> tuple[float, object]:
    est = estimate_source(anoms)
    assert est is not None
    return haversine_km((est.lat, est.lon), truth), est


# --------------------------------------------------------------------------
# haversine_km
# --------------------------------------------------------------------------

def test_haversine_zero_distance():
    assert haversine_km((51.47, -0.46), (51.47, -0.46)) == pytest.approx(0.0, abs=1e-9)


def test_haversine_one_degree_of_latitude():
    # A degree of latitude is ~111.19 km on a sphere of this radius.
    assert haversine_km((0.0, 0.0), (1.0, 0.0)) == pytest.approx(111.19, abs=0.1)


def test_haversine_known_long_leg():
    # London Heathrow -> New York JFK, ~5555 km great circle.
    assert haversine_km((51.4706, -0.4619), (40.6413, -73.7781)) == pytest.approx(5555, rel=0.01)


def test_haversine_is_symmetric():
    a, b = (60.318, 24.963), (25.250, 55.371)
    assert haversine_km(a, b) == pytest.approx(haversine_km(b, a), abs=1e-9)


def test_haversine_crosses_antimeridian():
    # 1 degree apart across the date line, near the equator: must be ~111 km,
    # not ~39 000 km the wrong way round.
    assert haversine_km((0.0, 179.5), (0.0, -179.5)) == pytest.approx(111.19, abs=0.1)


def test_haversine_poles():
    assert haversine_km((90.0, 0.0), (-90.0, 0.0)) == pytest.approx(math.pi * EARTH_R_KM, rel=1e-6)


# --------------------------------------------------------------------------
# line of sight
# --------------------------------------------------------------------------

def test_los_radius_matches_formula():
    # 3.57 * (sqrt(10) + sqrt(10000)) = 11.29 + 357.0
    assert los_radius_km(10000.0) == pytest.approx(3.57 * (math.sqrt(10) + 100.0), abs=1e-6)


def test_los_radius_grows_with_altitude():
    assert los_radius_km(1000.0) < los_radius_km(10000.0) < los_radius_km(13000.0)


def test_los_radius_defaults_when_altitude_missing():
    assert los_radius_km(None) == pytest.approx(los_radius_km(10000.0))


def test_los_radius_handles_ground_and_negative():
    assert los_radius_km(0.0) == pytest.approx(3.57 * math.sqrt(10), abs=1e-6)
    assert los_radius_km(-50.0) == pytest.approx(los_radius_km(0.0))


def test_bearing_cardinal_directions():
    assert bearing_deg((0.0, 0.0), (1.0, 0.0)) == pytest.approx(0.0, abs=0.01)
    assert bearing_deg((0.0, 0.0), (0.0, 1.0)) == pytest.approx(90.0, abs=0.01)
    assert bearing_deg((0.0, 0.0), (-1.0, 0.0)) == pytest.approx(180.0, abs=0.01)


# --------------------------------------------------------------------------
# estimate_source: degenerate inputs
# --------------------------------------------------------------------------

def test_empty_input_returns_none():
    assert estimate_source([]) is None


def test_single_aircraft_returns_none():
    """One aircraft cannot constrain a position, however many anomalies it
    reports. Returning a point here would look like a fix and would not be."""
    anoms = [
        Anomaly(icao24="aaaaaa", time=1000 + i * 10, lat=54.5 + i * 0.1, lon=20.5,
                alt_m=10000.0, kind="position_jump", severity=0.9, detail="sim")
        for i in range(12)
    ]
    assert estimate_source(anoms) is None


def test_two_aircraft_returns_none():
    anoms = [
        Anomaly(icao24=f"ac{i}", time=1000, lat=54.5 + i, lon=20.5,
                alt_m=10000.0, kind="position_jump", severity=0.9, detail="sim")
        for i in range(2)
    ]
    assert estimate_source(anoms) is None


def test_exactly_min_aircraft_returns_an_estimate():
    anoms = [
        Anomaly(icao24=f"ac{i}", time=1000, lat=54.5 + i * 0.5, lon=20.5 + i * 0.5,
                alt_m=10000.0, kind="position_jump", severity=0.9, detail="sim")
        for i in range(MIN_AIRCRAFT)
    ]
    est = estimate_source(anoms)
    assert est is not None
    assert est.n_anomalies == MIN_AIRCRAFT


# --------------------------------------------------------------------------
# estimate_source: localization quality
# --------------------------------------------------------------------------

BALTIC_SOURCE = (54.70, 20.50)      # illustrative location for the simulator


def test_clean_event_localizes_near_truth():
    anoms = simulate_event(BALTIC_SOURCE, n_aircraft=30, seed=1)
    assert len({a.icao24 for a in anoms}) >= MIN_AIRCRAFT
    err, est = localization_error_km(anoms, BALTIC_SOURCE)
    assert est.method == "los_intersection"
    assert err < 250.0, f"localization error {err:.1f} km"


def test_uncertainty_radius_contains_the_truth():
    """The honest property: the reported radius must actually cover the real
    source. An optimistic radius is worse than a large one."""
    for seed in range(8):
        anoms = simulate_event(BALTIC_SOURCE, n_aircraft=30, seed=seed)
        err, est = localization_error_km(anoms, BALTIC_SOURCE)
        assert err <= est.radius_km, (
            f"seed {seed}: error {err:.1f} km exceeds reported radius {est.radius_km:.1f} km"
        )


def test_localization_error_across_seeds(capsys):
    """Measure and print mean/max error. The printed table is the number
    quoted in NOTES_ahaan.md; run with -s to see it."""
    errors = []
    for seed in range(12):
        anoms = simulate_event(BALTIC_SOURCE, n_aircraft=30, seed=seed)
        err, est = localization_error_km(anoms, BALTIC_SOURCE)
        errors.append(err)
        with capsys.disabled():
            print(f"  seed {seed:2d}: n_ac={len({a.icao24 for a in anoms}):3d} "
                  f"err={err:7.1f} km  radius={est.radius_km:7.1f} km  "
                  f"conf={est.confidence:.3f}  {est.method}")
    mean_err = sum(errors) / len(errors)
    with capsys.disabled():
        print(f"  clean: mean {mean_err:.1f} km, max {max(errors):.1f} km, n={len(errors)}")
    assert mean_err < 150.0
    assert max(errors) < 300.0


def test_noisy_positions_still_localize(capsys):
    """Position noise can empty the strict intersection; the estimate must
    degrade to a partial intersection rather than fail or lie."""
    errors = []
    for seed in range(8):
        anoms = simulate_event(BALTIC_SOURCE, n_aircraft=30, noise_km=50.0, seed=seed)
        err, est = localization_error_km(anoms, BALTIC_SOURCE)
        errors.append(err)
        assert est.method in ("los_intersection", "los_partial_intersection")
        assert 0.0 <= est.confidence <= 1.0
    mean_err = sum(errors) / len(errors)
    with capsys.disabled():
        print(f"  noisy (50 km): mean {mean_err:.1f} km, max {max(errors):.1f} km, n={len(errors)}")
    assert mean_err < 200.0


def test_low_altitude_aircraft_give_a_tighter_fix():
    """Smaller line-of-sight discs constrain the source better. This is the
    core of the method, so it is worth pinning down."""
    high = simulate_event(BALTIC_SOURCE, n_aircraft=60, alt_m=11000.0,
                          scatter_km=300.0, seed=3)
    low = simulate_event(BALTIC_SOURCE, n_aircraft=60, alt_m=900.0,
                         scatter_km=300.0, seed=3)
    est_high = estimate_source(high)
    est_low = estimate_source(low)
    assert est_high is not None and est_low is not None
    assert est_low.radius_km < est_high.radius_km


def test_surrounding_geometry_beats_one_sided_geometry():
    """Aircraft on one side of the source give a far weaker fix than aircraft
    around it, and confidence must say so."""
    around = simulate_event(BALTIC_SOURCE, n_aircraft=40, seed=5,
                            bearings=(0.0, 360.0))
    one_side = simulate_event(BALTIC_SOURCE, n_aircraft=40, seed=5,
                              bearings=(0.0, 50.0))
    est_around = estimate_source(around)
    est_side = estimate_source(one_side)
    assert est_around is not None and est_side is not None
    assert est_around.confidence > est_side.confidence


def test_more_aircraft_raises_confidence():
    few = simulate_event(BALTIC_SOURCE, n_aircraft=6, scatter_km=250.0, seed=7)
    many = simulate_event(BALTIC_SOURCE, n_aircraft=60, scatter_km=250.0, seed=7)
    est_few, est_many = estimate_source(few), estimate_source(many)
    assert est_few is not None and est_many is not None
    assert est_many.confidence > est_few.confidence


# --------------------------------------------------------------------------
# estimate_source: contract and edge cases
# --------------------------------------------------------------------------

def test_result_fields_are_within_model_bounds():
    anoms = simulate_event(BALTIC_SOURCE, n_aircraft=30, seed=2)
    est = estimate_source(anoms)
    assert est is not None
    assert -90.0 <= est.lat <= 90.0
    assert -180.0 <= est.lon <= 180.0
    assert est.radius_km > 0.0
    assert 0.0 <= est.confidence <= 1.0
    assert est.n_anomalies == len(anoms)
    assert est.method in ("los_intersection", "los_partial_intersection", "weighted_centroid")


def test_is_deterministic():
    anoms = simulate_event(BALTIC_SOURCE, n_aircraft=30, seed=4)
    first = estimate_source(anoms)
    second = estimate_source(list(anoms))
    assert first is not None
    assert first.model_dump() == second.model_dump()


def test_input_order_does_not_change_the_result():
    anoms = simulate_event(BALTIC_SOURCE, n_aircraft=25, seed=6)
    forward = estimate_source(anoms)
    backward = estimate_source(list(reversed(anoms)))
    assert forward is not None
    assert forward.model_dump() == backward.model_dump()


def test_missing_altitude_is_handled():
    anoms = simulate_event(BALTIC_SOURCE, n_aircraft=30, seed=8)
    stripped = [a.model_copy(update={"alt_m": None}) for a in anoms]
    est = estimate_source(stripped)
    assert est is not None
    assert haversine_km((est.lat, est.lon), BALTIC_SOURCE) < 300.0


def test_event_across_the_antimeridian():
    """Longitude wrapping must not split the cluster or throw the centroid to
    the other side of the world."""
    source = (52.0, 179.6)
    anoms = simulate_event(source, n_aircraft=40, scatter_km=300.0, seed=9)
    lons = {round(a.lon) for a in anoms}
    assert any(v > 170 for v in lons) and any(v < -170 for v in lons), "fixture must straddle 180"
    err, est = localization_error_km(anoms, source)
    assert err < 300.0, f"antimeridian error {err:.1f} km"


def test_two_far_apart_sources_are_not_reported_confidently():
    """Anomalies from two clusters thousands of km apart have no common line
    of sight. The result must be flagged and low-confidence, not a confident
    point in the empty ocean between them."""
    a = simulate_event((54.7, 20.5), n_aircraft=20, scatter_km=200.0, seed=10)
    b = simulate_event((25.3, 51.6), n_aircraft=20, scatter_km=200.0, seed=11)
    b = [x.model_copy(update={"icao24": "z" + x.icao24[1:]}) for x in b]
    est = estimate_source(a + b)
    assert est is not None
    assert est.method in ("los_partial_intersection", "weighted_centroid")
    assert est.confidence < 0.6


def test_all_aircraft_at_one_point_gives_a_wide_region():
    """Co-located aircraft carry one disc's worth of information, so the
    region must be about one line-of-sight disc wide, not a pinpoint."""
    anoms = [
        Anomaly(icao24=f"ac{i}", time=1000 + i, lat=54.7, lon=20.5, alt_m=10000.0,
                kind="position_jump", severity=0.9, detail="sim")
        for i in range(10)
    ]
    est = estimate_source(anoms)
    assert est is not None
    assert est.radius_km > 200.0
    assert est.confidence < 0.6
