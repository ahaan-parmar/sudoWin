"""Tests for gnsswatch.hotzones and gnsswatch.airports (P3).

Inline fixtures only -- nothing here depends on another person's module. The
shipped seed file is exercised too, including the requirement that it is
labelled illustrative and never reported as observed.
"""
from __future__ import annotations

import json
import math

import pytest

from gnsswatch import airports
from gnsswatch.hotzones import (
    SEED_PATH,
    build_hotzones,
    load_hotzones,
    route_points,
    route_risk,
    save_hotzones,
)
from gnsswatch.locate import haversine_km
from gnsswatch.models import Anomaly

T0 = 1_700_000_000          # a fixed unix time; hour of day comes out of it


def anom(icao: str, lat: float, lon: float, hour: int = 0,
         severity: float = 0.8, kind: str = "position_jump") -> Anomaly:
    """Anomaly at a chosen hour of day UTC."""
    base = (T0 // 86400) * 86400
    return Anomaly(icao24=icao, time=base + hour * 3600, lat=lat, lon=lon,
                   alt_m=10000.0, kind=kind, severity=severity, detail="test fixture")


# --------------------------------------------------------------------------
# airports
# --------------------------------------------------------------------------

def test_required_airports_are_present():
    for icao in ("VOBL", "VIDP", "EFHK", "EGLL", "EDDF", "OMDB", "ESSA"):
        assert airports.get(icao) is not None, icao


def test_airport_table_covers_the_reported_regions():
    regions = {a.region for a in airports.list_airports()}
    assert {"baltic", "black_sea", "east_med", "persian_gulf"} <= regions


def test_airport_coordinates_are_in_range_and_keys_match():
    for icao, ap in airports.AIRPORTS.items():
        assert icao == ap.icao
        assert len(icao) == 4 and icao.isupper()
        assert -90.0 <= ap.lat <= 90.0
        assert -180.0 <= ap.lon <= 180.0


def test_spot_check_known_coordinates():
    """Sanity-check a few against independently known positions, so a typo in
    the table cannot pass silently."""
    for icao, lat, lon in [
        ("EGLL", 51.47, -0.46),     # London Heathrow
        ("EDDF", 50.03, 8.56),      # Frankfurt
        ("OMDB", 25.25, 55.37),     # Dubai
        ("VIDP", 28.56, 77.10),     # Delhi
        ("VOBL", 13.20, 77.71),     # Bengaluru
        ("EFHK", 60.32, 24.96),     # Helsinki
        ("ESSA", 59.65, 17.93),     # Stockholm Arlanda
    ]:
        ap = airports.get(icao)
        assert haversine_km((ap.lat, ap.lon), (lat, lon)) < 5.0, icao


def test_resolve_accepts_icao_case_insensitively():
    assert airports.resolve("efhk") == airports.resolve("EFHK")


def test_resolve_follows_legacy_aliases():
    assert airports.resolve("OKBK") == airports.resolve("OKKK")


def test_resolve_accepts_lat_lon_string():
    assert airports.resolve("54.5,20.5") == (54.5, 20.5)
    assert airports.resolve(" -33.9 , 151.2 ") == (-33.9, 151.2)


def test_resolve_rejects_nonsense():
    for bad in ("", "ZZZZ", "not a place", "91.0,0.0", "0.0,181.0", "12,"):
        with pytest.raises(ValueError):
            airports.resolve(bad)


# --------------------------------------------------------------------------
# build_hotzones
# --------------------------------------------------------------------------

def test_build_hotzones_on_empty_history():
    hz = build_hotzones([])
    assert hz["cells"] == []
    assert hz["basis"] == "observed"
    assert hz["n_anomalies"] == 0


def test_build_hotzones_buckets_by_cell_and_hour():
    history = [
        anom("aaa", 54.2, 20.3, hour=5),
        anom("bbb", 54.8, 20.9, hour=5),     # same 1-degree cell and hour
        anom("ccc", 54.4, 20.1, hour=9),     # same cell, different hour
    ]
    cells = build_hotzones(history)["cells"]
    assert len(cells) == 2
    by_hour = {c["hour_utc"]: c for c in cells}
    assert by_hour[5]["n_anomalies"] == 2
    assert by_hour[5]["n_aircraft"] == 2
    assert by_hour[9]["n_anomalies"] == 1


def test_build_hotzones_cell_centres_are_on_the_half_degree():
    hz = build_hotzones([anom("aaa", 54.2, 20.3, hour=0)], cell_deg=1.0)
    cell = hz["cells"][0]
    assert cell["lat"] == pytest.approx(54.5)
    assert cell["lon"] == pytest.approx(20.5)


def test_build_hotzones_honours_cell_deg():
    history = [anom("aaa", 54.2, 20.2, hour=0), anom("bbb", 54.8, 20.8, hour=0)]
    assert len(build_hotzones(history, cell_deg=1.0)["cells"]) == 1
    assert len(build_hotzones(history, cell_deg=0.25)["cells"]) == 2


def test_build_hotzones_counts_distinct_aircraft_not_reports():
    history = [anom("aaa", 54.5, 20.5, hour=1) for _ in range(6)]
    cell = build_hotzones(history)["cells"][0]
    assert cell["n_anomalies"] == 6
    assert cell["n_aircraft"] == 1


def test_build_hotzones_keeps_max_severity_and_bounded_score():
    history = [
        anom("aaa", 54.5, 20.5, hour=1, severity=0.3),
        anom("bbb", 54.5, 20.5, hour=1, severity=0.95),
    ]
    cell = build_hotzones(history)["cells"][0]
    assert cell["max_severity"] == pytest.approx(0.95)
    assert 0.0 <= cell["score"] <= 1.0


def test_build_hotzones_score_rises_with_more_aircraft():
    one = build_hotzones([anom("aaa", 54.5, 20.5, hour=1, severity=0.5)])
    many = build_hotzones(
        [anom(f"ac{i}", 54.5, 20.5, hour=1, severity=0.5) for i in range(6)]
    )
    assert many["cells"][0]["score"] > one["cells"][0]["score"]


def test_build_hotzones_is_deterministic():
    history = [anom(f"ac{i}", 54.0 + i * 0.4, 20.0 + i * 0.4, hour=i % 24) for i in range(20)]
    assert build_hotzones(history) == build_hotzones(list(reversed(history)))


def test_build_hotzones_rejects_bad_cell_deg():
    with pytest.raises(ValueError):
        build_hotzones([], cell_deg=0.0)


def test_build_hotzones_output_is_json_serialisable():
    hz = build_hotzones([anom("aaa", 54.5, 20.5, hour=3)])
    assert json.loads(json.dumps(hz)) == hz


# --------------------------------------------------------------------------
# save / load
# --------------------------------------------------------------------------

def test_save_load_round_trip(tmp_path):
    hz = build_hotzones([anom(f"ac{i}", 54.0 + i, 20.0 + i, hour=i) for i in range(5)])
    path = tmp_path / "nested" / "hotzones.json"
    save_hotzones(hz, str(path))
    assert path.exists()
    assert load_hotzones(str(path)) == hz


def test_load_missing_file_raises(tmp_path):
    """Missing data must not quietly score every route as safe."""
    with pytest.raises(FileNotFoundError):
        load_hotzones(str(tmp_path / "nope.json"))


def test_load_rejects_a_file_that_is_not_hotzones(tmp_path):
    path = tmp_path / "junk.json"
    path.write_text('{"hello": 1}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_hotzones(str(path))


# --------------------------------------------------------------------------
# the shipped seed file
# --------------------------------------------------------------------------

def test_seed_file_loads_and_is_labelled_illustrative():
    hz = load_hotzones(SEED_PATH)
    assert hz["basis"] == "illustrative_seed"
    label = hz["label"].upper()
    assert "ILLUSTRATIVE" in label and "NOT MEASURED" in label


def test_seed_file_covers_the_four_reported_regions():
    hz = load_hotzones(SEED_PATH)
    regions = {c.get("region") for c in hz["cells"]}
    assert {"baltic", "black_sea", "east_med", "persian_gulf"} <= regions


def test_seed_cells_are_well_formed():
    for cell in load_hotzones(SEED_PATH)["cells"]:
        assert -90.0 <= cell["lat"] <= 90.0
        assert -180.0 <= cell["lon"] <= 180.0
        assert 0.0 <= cell["score"] <= 1.0
        assert 0.0 <= cell["max_severity"] <= 1.0
        assert cell["hour_utc"] is None or 0 <= cell["hour_utc"] <= 23


def test_seed_always_reports_illustrative_basis():
    """The contract requirement: a route scored against the seed must say so."""
    hz = load_hotzones(SEED_PATH)
    risk = route_risk("EFHK", "EGLL", 14, hz)
    assert risk.basis == "illustrative_seed"
    assert any("ILLUSTRATIVE" in line.upper() for line in risk.advice)


# --------------------------------------------------------------------------
# route_points
# --------------------------------------------------------------------------

def test_route_points_count_and_endpoints():
    pts = route_points("EFHK", "EGLL", n=50)
    assert len(pts) == 50
    assert haversine_km(pts[0], airports.resolve("EFHK")) < 1.0
    assert haversine_km(pts[-1], airports.resolve("EGLL")) < 1.0


def test_route_points_accepts_lat_lon_endpoints():
    pts = route_points("54.5,20.5", "55.5,21.5", n=3)
    assert haversine_km(pts[0], (54.5, 20.5)) < 1.0
    assert haversine_km(pts[-1], (55.5, 21.5)) < 1.0


def test_route_points_are_evenly_spaced():
    pts = route_points("EFHK", "OMDB", n=21)
    legs = [haversine_km(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    assert max(legs) - min(legs) < 1.0


def test_route_points_follow_the_great_circle_not_a_straight_line():
    """Between two points at the same high latitude, the great circle bulges
    poleward. A naive lat/lon interpolation would not, and would miss hot
    cells on the real track."""
    pts = route_points("60.0,10.0", "60.0,60.0", n=51)
    assert max(p[0] for p in pts) > 62.0


def test_route_points_along_the_equator_stay_on_it():
    pts = route_points("0.0,0.0", "0.0,40.0", n=11)
    assert all(abs(p[0]) < 1e-6 for p in pts)


def test_route_points_same_origin_and_destination():
    pts = route_points("EFHK", "EFHK", n=5)
    assert len(pts) == 5
    assert all(haversine_km(p, airports.resolve("EFHK")) < 1.0 for p in pts)


def test_route_points_n_of_one():
    assert len(route_points("EFHK", "EGLL", n=1)) == 1


def test_route_points_rejects_bad_n():
    with pytest.raises(ValueError):
        route_points("EFHK", "EGLL", n=0)


def test_route_points_rejects_antipodal_endpoints():
    with pytest.raises(ValueError):
        route_points("0.0,0.0", "0.0,180.0", n=10)


def test_route_points_rejects_unknown_airport():
    with pytest.raises(ValueError):
        route_points("ZZZZ", "EGLL", n=10)


# --------------------------------------------------------------------------
# route_risk
# --------------------------------------------------------------------------

def hz_at(lat: float, lon: float, hour, score: float = 0.9,
          basis: str = "observed") -> dict:
    """A one-cell hot-zone dict, for pinning down route_risk precisely."""
    return {
        "version": 1, "cell_deg": 1.0, "basis": basis, "label": "test fixture",
        "n_anomalies": 10, "n_aircraft": 4,
        "cells": [{"lat": lat, "lon": lon, "hour_utc": hour, "n_anomalies": 10,
                   "n_aircraft": 4, "max_severity": 0.9, "score": score}],
    }


def test_route_far_from_any_cell_scores_zero():
    risk = route_risk("VOBL", "VIDP", 6, hz_at(54.5, 20.5, None))
    assert risk.score == 0.0
    assert risk.cells == []
    assert any("No hot cells" in line for line in risk.advice)


def test_route_through_a_cell_scores_high():
    # A cell sitting on the Helsinki -> London track.
    risk = route_risk("EFHK", "EGLL", 12, hz_at(59.5, 20.0, None))
    assert risk.score > 0.5
    assert len(risk.cells) == 1


def test_cells_outside_the_corridor_are_excluded():
    hz = hz_at(45.0, 20.0, None)       # far south of the Helsinki-London track
    assert route_risk("EFHK", "EGLL", 12, hz, corridor_km=100.0).cells == []


def test_widening_the_corridor_can_only_add_cells():
    hz = load_hotzones(SEED_PATH)
    narrow = route_risk("EFHK", "EGLL", 14, hz, corridor_km=50.0)
    wide = route_risk("EFHK", "EGLL", 14, hz, corridor_km=600.0)
    assert len(wide.cells) >= len(narrow.cells)
    assert wide.score >= narrow.score


def test_hour_filter_selects_cells():
    hz = hz_at(59.5, 20.0, 14)
    assert route_risk("EFHK", "EGLL", 14, hz).score > 0.0
    assert route_risk("EFHK", "EGLL", 3, hz).score == 0.0


def test_null_hour_applies_at_every_hour():
    hz = hz_at(59.5, 20.0, None)
    scores = {route_risk("EFHK", "EGLL", h, hz).score for h in range(24)}
    assert len(scores) == 1 and scores.pop() > 0.0


def test_hour_changes_the_answer_on_the_seed_data():
    """The demo relies on this: the same route is riskier at some hours."""
    hz = load_hotzones(SEED_PATH)
    assert route_risk("ESSA", "EPWA", 14, hz).score > route_risk("ESSA", "EPWA", 3, hz).score


def test_more_cells_raise_the_score():
    one = hz_at(59.5, 20.0, None)
    two = hz_at(59.5, 20.0, None)
    two["cells"].append({"lat": 57.5, "lon": 15.5, "hour_utc": None, "n_anomalies": 5,
                         "n_aircraft": 3, "max_severity": 0.7, "score": 0.7})
    assert route_risk("EFHK", "EGLL", 12, two).score >= route_risk("EFHK", "EGLL", 12, one).score


def test_a_cell_on_track_outweighs_one_at_the_corridor_edge():
    on_track = route_risk("0.0,0.0", "0.0,20.0", 12, hz_at(0.0, 10.0, None), corridor_km=200.0)
    at_edge = route_risk("0.0,0.0", "0.0,20.0", 12, hz_at(1.7, 10.0, None), corridor_km=200.0)
    assert on_track.score > at_edge.score


def test_score_is_bounded_even_with_many_cells():
    hz = hz_at(59.5, 20.0, None)
    hz["cells"] = [dict(hz["cells"][0], lon=lon) for lon in range(0, 25)]
    risk = route_risk("EFHK", "EGLL", 12, hz)
    assert 0.0 <= risk.score <= 1.0


def test_basis_observed_is_passed_through():
    risk = route_risk("EFHK", "EGLL", 12, hz_at(59.5, 20.0, None, basis="observed"))
    assert risk.basis == "observed"
    assert any("observed" in line for line in risk.advice)


def test_unknown_basis_is_treated_as_illustrative():
    """Conservative direction: never claim a measurement we cannot back up."""
    hz = hz_at(59.5, 20.0, None, basis="something_else")
    assert route_risk("EFHK", "EGLL", 12, hz).basis == "illustrative_seed"


def test_advice_is_non_empty_and_deterministic():
    hz = load_hotzones(SEED_PATH)
    first = route_risk("EFHK", "EGLL", 14, hz)
    second = route_risk("EFHK", "EGLL", 14, hz)
    assert first.advice and first.advice == second.advice
    assert first.model_dump() == second.model_dump()


def test_advice_names_a_non_gnss_approach_when_the_route_is_affected():
    risk = route_risk("EFHK", "EGLL", 12, hz_at(59.5, 20.0, None))
    joined = " ".join(risk.advice)
    assert "GNSS degradation" in joined
    assert "brief" in joined.lower()
    assert "ILS" in joined


def test_advice_lines_are_short():
    """These are read aloud in a brief; keep them to one line each."""
    hz = load_hotzones(SEED_PATH)
    for line in route_risk("EFHK", "EGLL", 14, hz).advice:
        assert len(line) <= 200, line


def test_route_risk_echoes_its_inputs():
    hz = load_hotzones(SEED_PATH)
    risk = route_risk("EFHK", "EGLL", 9, hz)
    assert (risk.origin, risk.destination, risk.hour_utc) == ("EFHK", "EGLL", 9)


def test_route_risk_rejects_bad_hour():
    hz = load_hotzones(SEED_PATH)
    for bad in (-1, 24, 99):
        with pytest.raises(ValueError):
            route_risk("EFHK", "EGLL", bad, hz)


def test_route_risk_rejects_bad_corridor():
    with pytest.raises(ValueError):
        route_risk("EFHK", "EGLL", 12, load_hotzones(SEED_PATH), corridor_km=0.0)


def test_route_risk_accepts_lat_lon_endpoints():
    risk = route_risk("60.3,25.0", "51.5,-0.5", 12, hz_at(59.5, 20.0, None))
    assert risk.score > 0.0


def test_route_risk_on_zones_built_from_anomalies():
    """End to end within this module: anomalies -> hot zones -> route risk."""
    history = [anom(f"ac{i}", 59.5 + i * 0.05, 20.0 + i * 0.05, hour=12) for i in range(6)]
    hz = build_hotzones(history)
    risk = route_risk("EFHK", "EGLL", 12, hz)
    assert risk.basis == "observed"
    assert risk.score > 0.0
    assert risk.cells
    assert route_risk("EFHK", "EGLL", 5, hz).score == 0.0      # different hour
