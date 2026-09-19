"""Tests for the simulated interference injectors.

Three things matter here: the input snapshot is never touched, the recorded
ground truth matches exactly what was changed, and the same seed gives the same
result.
"""
from __future__ import annotations

import collections

import pytest

from gnsswatch.data import _haversine_km, synthetic_baseline
from gnsswatch.simulate import inject_jam, inject_spoof

BBOX = (8.0, 68.0, 24.0, 88.0)
SOURCE = (19.0, 78.0)
RADIUS_KM = 350.0
MODES = ("teleport", "drift", "attractor")


@pytest.fixture(scope="module")
def clean():
    return synthetic_baseline(BBOX, n_aircraft=40, duration_s=1800, step_s=10, seed=0)


@pytest.fixture(scope="module")
def window(clean):
    return clean.t_start + 600, clean.t_start + 1500


def _index(snap):
    return {(st.icao24, st.time): st for st in snap.states}


def _tracks(snap):
    out = collections.defaultdict(list)
    for st in snap.states:
        out[st.icao24].append(st)
    for track in out.values():
        track.sort(key=lambda s: s.time)
    return out


# --- input is never mutated -------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
def test_spoof_does_not_mutate_input(clean, window, mode):
    before = clean.model_dump()
    inject_spoof(clean, SOURCE, RADIUS_KM, window[0], window[1], mode=mode,
                 target=(20.0, 79.0), seed=1)
    assert clean.model_dump() == before


def test_jam_does_not_mutate_input(clean, window):
    before = clean.model_dump()
    inject_jam(clean, SOURCE, RADIUS_KM, window[0], window[1], seed=1)
    assert clean.model_dump() == before


# --- ground truth matches what changed --------------------------------------

@pytest.mark.parametrize("mode", MODES)
def test_spoof_ground_truth_matches_changes(clean, window, mode):
    out = inject_spoof(clean, SOURCE, RADIUS_KM, window[0], window[1], mode=mode,
                       target=(20.0, 79.0), seed=1)
    ev = out.injected
    assert ev is not None
    assert out.kind == "synthetic"
    assert ev.mode == mode
    assert (ev.source_lat, ev.source_lon) == SOURCE
    assert ev.radius_km == RADIUS_KM
    assert (ev.t_start, ev.t_end) == window
    assert ev.affected_icao24, "the event should hit at least one aircraft"
    assert ev.affected_icao24 == sorted(set(ev.affected_icao24))

    before, after = _index(clean), _index(out)
    assert before.keys() == after.keys(), "spoofing must not add or drop reports"

    changed = {k[0] for k in before if
               (before[k].lat, before[k].lon) != (after[k].lat, after[k].lon)}
    assert changed == set(ev.affected_icao24)

    for key, old in before.items():
        new = after[key]
        if key[0] not in changed:
            assert new.model_dump() == old.model_dump()
            continue
        moved = (new.lat, new.lon) != (old.lat, old.lon)
        if moved:
            # only reports inside the radius during the window are touched
            assert ev.t_start <= old.time <= ev.t_end
            assert _haversine_km((old.lat, old.lon), SOURCE) <= RADIUS_KM
        # the aircraft really is where it always was: pressure altitude and
        # true movement are untouched
        assert new.baro_alt_m == old.baro_alt_m
        assert new.velocity_ms == old.velocity_ms
        assert new.track_deg == old.track_deg
        assert new.on_ground == old.on_ground


def test_jam_ground_truth_matches_dropouts(clean, window):
    out = inject_jam(clean, SOURCE, RADIUS_KM, window[0], window[1], seed=1)
    ev = out.injected
    assert ev is not None and ev.mode == "dropout"
    assert out.kind == "synthetic"
    assert ev.affected_icao24

    before, after = _index(clean), _index(out)
    assert set(after) < set(before), "jamming should remove reports"
    dropped = set(before) - set(after)
    assert {k[0] for k in dropped} == set(ev.affected_icao24)
    for icao, t in dropped:
        old = before[(icao, t)]
        assert ev.t_start <= t <= ev.t_end
        assert _haversine_km((old.lat, old.lon), SOURCE) <= RADIUS_KM
    # everything that survived is byte-identical
    for key, new in after.items():
        assert new.model_dump() == before[key].model_dump()


def test_jam_leaves_a_gap_others_do_not_have(clean, window):
    out = inject_jam(clean, SOURCE, RADIUS_KM, window[0], window[1], seed=1)
    affected = set(out.injected.affected_icao24)
    tracks = _tracks(out)
    gapped = set()
    for icao, track in tracks.items():
        if any(b.time - a.time > 30 for a, b in zip(track, track[1:])):
            gapped.add(icao)
    # every gap belongs to an affected aircraft; aircraft that flew out of the
    # jammed area mid-window get a gap, the ones still inside at t_end do not
    assert gapped <= affected


# --- seeded determinism -----------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
def test_spoof_is_deterministic(clean, window, mode):
    kw = dict(mode=mode, target=(20.0, 79.0))
    a = inject_spoof(clean, SOURCE, RADIUS_KM, window[0], window[1], seed=2, **kw)
    b = inject_spoof(clean, SOURCE, RADIUS_KM, window[0], window[1], seed=2, **kw)
    c = inject_spoof(clean, SOURCE, RADIUS_KM, window[0], window[1], seed=9, **kw)
    assert a.model_dump() == b.model_dump()
    assert a.model_dump() != c.model_dump()


def test_jam_is_deterministic(clean, window):
    a = inject_jam(clean, SOURCE, RADIUS_KM, window[0], window[1], seed=2)
    b = inject_jam(clean, SOURCE, RADIUS_KM, window[0], window[1], seed=2)
    assert a.model_dump() == b.model_dump()


# --- each mode behaves the way its name says --------------------------------

def test_teleport_makes_an_impossible_jump(clean, window):
    out = inject_spoof(clean, SOURCE, RADIUS_KM, window[0], window[1],
                       mode="teleport", seed=1)
    affected = set(out.injected.affected_icao24)
    worst = 0.0
    for icao, track in _tracks(out).items():
        if icao not in affected:
            continue
        for a, b in zip(track, track[1:]):
            worst = max(worst, _haversine_km((a.lat, a.lon), (b.lat, b.lon)) * 1000.0
                        / (b.time - a.time))
    assert worst > 1000.0, "a teleport should be far outside any aircraft speed"


def test_drift_offset_grows(clean, window):
    out = inject_spoof(clean, SOURCE, RADIUS_KM, window[0], window[1],
                       mode="drift", seed=1)
    before = _index(clean)
    icao = out.injected.affected_icao24[0]
    offsets = []
    for st in _tracks(out)[icao]:
        old = before[(icao, st.time)]
        if window[0] <= st.time <= window[1]:
            offsets.append(_haversine_km((old.lat, old.lon), (st.lat, st.lon)))
    assert offsets[0] < 1.0, "the drift starts from nothing"
    assert offsets[-1] > offsets[len(offsets) // 2] > offsets[0]


def test_attractor_pulls_positions_toward_target(clean, window):
    target = (20.0, 79.0)
    out = inject_spoof(clean, SOURCE, RADIUS_KM, window[0], window[1],
                       mode="attractor", target=target, seed=1)
    assert (out.injected.target_lat, out.injected.target_lon) == target
    before = _index(clean)
    for icao in out.injected.affected_icao24:
        # an aircraft that flies out of the footprint snaps back to its true
        # position, so compare the last report that was actually spoofed
        moved = [s for s in _tracks(out)[icao]
                 if (s.lat, s.lon) != (before[(icao, s.time)].lat, before[(icao, s.time)].lon)]
        assert moved
        last = moved[-1]
        old = before[(icao, last.time)]
        assert (_haversine_km((last.lat, last.lon), target)
                < _haversine_km((old.lat, old.lon), target))


def test_spoof_moves_gnss_altitude_but_not_barometric(clean, window):
    out = inject_spoof(clean, SOURCE, RADIUS_KM, window[0], window[1],
                       mode="teleport", seed=1)
    before = _index(clean)
    worst = 0.0
    for key, new in _index(out).items():
        old = before[key]
        assert new.baro_alt_m == old.baro_alt_m
        worst = max(worst, abs(new.geo_alt_m - new.baro_alt_m))
    assert worst > 200.0, "a spoofed fix should open a baro/GNSS gap"


def test_target_defaults_to_source(clean, window):
    out = inject_spoof(clean, SOURCE, RADIUS_KM, window[0], window[1],
                       mode="attractor", seed=1)
    assert (out.injected.target_lat, out.injected.target_lon) == SOURCE


# --- naming and argument checks ---------------------------------------------

def test_default_names(clean, window):
    assert inject_spoof(clean, SOURCE, RADIUS_KM, *window, mode="drift").name \
        == f"{clean.name}_drift"
    assert inject_jam(clean, SOURCE, RADIUS_KM, *window).name == f"{clean.name}_jam"
    assert inject_jam(clean, SOURCE, RADIUS_KM, *window, name="demo").name == "demo"


def test_bad_arguments(clean, window):
    with pytest.raises(ValueError):
        inject_spoof(clean, SOURCE, RADIUS_KM, *window, mode="wobble")
    with pytest.raises(ValueError):
        inject_spoof(clean, SOURCE, -1.0, *window)
    with pytest.raises(ValueError):
        inject_spoof(clean, SOURCE, RADIUS_KM, window[1], window[0])
    with pytest.raises(ValueError):
        inject_jam(clean, SOURCE, 0.0, *window)


def test_out_of_range_event_affects_nobody(clean, window):
    out = inject_spoof(clean, (-40.0, -120.0), 50.0, window[0], window[1], seed=1)
    assert out.injected.affected_icao24 == []
    assert out.model_dump()["states"] == clean.model_dump()["states"]
