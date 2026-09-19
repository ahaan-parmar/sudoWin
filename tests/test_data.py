"""Tests for the synthetic baseline, snapshot storage and the live parser.

These use inline fixtures only. Nothing here needs another module or the
network.
"""
from __future__ import annotations

import collections

import pytest

from gnsswatch.data import (
    BASE_TIME,
    _bearing_deg,
    _haversine_km,
    _parse_states,
    fetch_live,
    list_snapshots,
    load_snapshot,
    save_snapshot,
    synthetic_baseline,
)

BBOX = (8.0, 68.0, 24.0, 88.0)  # roughly the Indian subcontinent


def _by_aircraft(snap):
    tracks = collections.defaultdict(list)
    for st in snap.states:
        tracks[st.icao24].append(st)
    for track in tracks.values():
        track.sort(key=lambda s: s.time)
    return tracks


def reference_anomalies(snap, step_s: int = 10) -> list[str]:
    """A deliberately generous stand-in for the detector in CONTRACT.md.

    P1 cannot import the detector module, so the promise that the baseline is
    clean is checked here against the same list of signals. Thresholds are
    loose on purpose. Anything this flags would certainly be flagged by a real
    detector.
    """
    found: list[str] = []
    tracks = _by_aircraft(snap)

    seen: set[tuple[str, int]] = set()
    for st in snap.states:
        key = (st.icao24, st.time)
        if key in seen:
            found.append(f"duplicate_id {key}")
        seen.add(key)

    times_with_traffic = sorted({st.time for st in snap.states})

    for icao, track in tracks.items():
        for st in track:
            if st.velocity_ms is not None and not (60.0 <= st.velocity_ms <= 340.0):
                found.append(f"impossible_speed {icao} {st.velocity_ms}")
            if st.baro_alt_m is not None and st.geo_alt_m is not None:
                if abs(st.geo_alt_m - st.baro_alt_m) > 200.0:
                    found.append(f"baro_geo_mismatch {icao} {st.geo_alt_m - st.baro_alt_m}")
            if st.nic is not None and st.nic < 6:
                found.append(f"integrity_drop nic {icao}")
            if st.nacp is not None and st.nacp < 7:
                found.append(f"integrity_drop nacp {icao}")

        for a, b in zip(track, track[1:]):
            dt = b.time - a.time
            if dt <= 0:
                continue
            implied = _haversine_km((a.lat, a.lon), (b.lat, b.lon)) * 1000.0 / dt
            if implied > 400.0:
                found.append(f"position_jump {icao} {implied:.0f}")
            if a.velocity_ms is not None and abs(implied - a.velocity_ms) > 25.0:
                found.append(f"velocity_mismatch {icao} {implied - a.velocity_ms:.1f}")
            if a.track_deg is not None:
                brg = _bearing_deg((a.lat, a.lon), (b.lat, b.lon))
                if abs((brg - a.track_deg + 180.0) % 360.0 - 180.0) > 15.0:
                    found.append(f"track_mismatch {icao}")
            # a gap while the rest of the region keeps reporting
            if dt > 3 * step_s:
                between = [t for t in times_with_traffic if a.time < t < b.time]
                if between:
                    found.append(f"signal_dropout {icao} {dt}s")
    return found


# --- baseline ---------------------------------------------------------------

def test_baseline_shape():
    snap = synthetic_baseline(BBOX, n_aircraft=12, duration_s=600, step_s=10, seed=7)
    tracks = _by_aircraft(snap)
    assert len(tracks) == 12
    assert {len(t) for t in tracks.values()} == {61}
    assert snap.kind == "synthetic"
    assert snap.injected is None
    assert snap.t_start == BASE_TIME
    assert snap.t_end == BASE_TIME + 600
    assert snap.states == sorted(snap.states, key=lambda s: (s.icao24, s.time))


def test_baseline_is_clean():
    snap = synthetic_baseline(BBOX, n_aircraft=40, duration_s=1800, step_s=10, seed=0)
    assert reference_anomalies(snap) == []


@pytest.mark.parametrize("seed", [0, 3, 11])
def test_baseline_clean_across_seeds(seed):
    snap = synthetic_baseline(BBOX, n_aircraft=15, duration_s=900, step_s=10, seed=seed)
    assert reference_anomalies(snap) == []


def test_baseline_is_deterministic():
    a = synthetic_baseline(BBOX, n_aircraft=8, duration_s=300, seed=5)
    b = synthetic_baseline(BBOX, n_aircraft=8, duration_s=300, seed=5)
    c = synthetic_baseline(BBOX, n_aircraft=8, duration_s=300, seed=6)
    assert a.model_dump() == b.model_dump()
    assert a.model_dump() != c.model_dump()


def test_baseline_is_realistic():
    snap = synthetic_baseline(BBOX, n_aircraft=25, duration_s=1200, seed=2)
    for st in snap.states:
        assert 9000.0 - 50 <= st.baro_alt_m <= 12000.0 + 50
        assert 10.0 <= abs(st.geo_alt_m - st.baro_alt_m) <= 100.0
        assert st.on_ground is False
        assert st.position_source == "adsb"
    speeds = [st.velocity_ms for st in snap.states]
    assert 190.0 < min(speeds) and max(speeds) < 260.0


def test_baseline_stays_in_bbox():
    snap = synthetic_baseline(BBOX, n_aircraft=30, duration_s=1800, seed=4)
    lat_min, lon_min, lat_max, lon_max = BBOX
    for st in snap.states:
        assert lat_min <= st.lat <= lat_max
        assert lon_min <= st.lon <= lon_max


def test_baseline_rejects_bad_args():
    with pytest.raises(ValueError):
        synthetic_baseline(BBOX, n_aircraft=0)
    with pytest.raises(ValueError):
        synthetic_baseline(BBOX, step_s=0)
    with pytest.raises(ValueError):
        synthetic_baseline(BBOX, duration_s=5, step_s=10)


# --- storage ----------------------------------------------------------------

def test_save_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("GNSSWATCH_DATA_DIR", str(tmp_path))
    snap = synthetic_baseline(BBOX, n_aircraft=5, duration_s=300, seed=1, name="rt")
    path = save_snapshot(snap)
    assert path.endswith("rt.json")
    for key in ("rt", "rt.json", path):
        assert load_snapshot(key).model_dump() == snap.model_dump()


def test_save_to_explicit_path(tmp_path):
    snap = synthetic_baseline(BBOX, n_aircraft=3, duration_s=120, seed=1, name="x")
    target = tmp_path / "nested" / "custom.json"
    path = save_snapshot(snap, str(target))
    assert path == str(target)
    assert load_snapshot(str(target)).name == "x"


def test_list_snapshots(tmp_path, monkeypatch):
    monkeypatch.setenv("GNSSWATCH_DATA_DIR", str(tmp_path))
    assert list_snapshots() == []
    for n in ("b", "a"):
        save_snapshot(synthetic_baseline(BBOX, n_aircraft=2, duration_s=60, seed=1, name=n))
    assert list_snapshots() == ["a", "b"]


def test_load_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("GNSSWATCH_DATA_DIR", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        load_snapshot("nope")


def test_committed_baseline_loads_and_is_clean():
    """The snapshot the offline demo depends on."""
    snap = load_snapshot("baseline_clean")
    assert snap.kind == "synthetic"
    assert snap.injected is None
    assert reference_anomalies(snap) == []


# --- live feed parsing ------------------------------------------------------

def _row(icao="abc123", t=1700000000, lat=12.0, lon=77.0):
    # OpenSky /states/all field order
    return [icao, "AIC101  ", "India", t, t, lon, lat, 10000.0, False,
            230.0, 90.0, 0.0, None, 10040.0, "1000", False, 0, 0]


def test_parse_states_skips_and_dedups():
    seen: set[tuple[str, int]] = set()
    rows = [
        _row(),
        _row(),                                    # exact duplicate
        _row(t=1700000010),                        # same aircraft, later
        [None] * 18,                               # no icao24
        ["dead01", "X", "C", 1, 1, None, None, 0, False, 0, 0, 0, None, 0, "", False, 0, 0],
        ["short"],                                 # truncated row
    ]
    out = _parse_states(rows, seen)
    assert [s.time for s in out] == [1700000000, 1700000010]
    assert out[0].icao24 == "abc123"
    assert out[0].callsign == "AIC101"
    assert out[0].lat == 12.0 and out[0].lon == 77.0
    assert out[0].position_source == "adsb"
    assert out[0].nic is None and out[0].nacp is None


def test_fetch_live_survives_network_failure(monkeypatch):
    """A dead network must give an empty snapshot, not a traceback."""
    import httpx

    class DeadClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            raise httpx.ConnectError("offline")

        def post(self, *a, **k):
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "Client", DeadClient)
    snap = fetch_live(BBOX, polls=2, interval_s=0, name="dead")
    assert snap.states == []
    assert snap.kind == "real"
    assert snap.name == "dead"
