"""Tests for gnsswatch.detect. Inline fixtures only; the P1 integration test skips if P1 is absent."""
from __future__ import annotations

import math
import time

import numpy as np
import pytest

from gnsswatch.detect import DetectConfig, detect, region_summary, score_flights
from gnsswatch.models import Anomaly, Snapshot, StateVector

T0 = 1_780_000_000
BBOX = (45.0, 5.0, 65.0, 35.0)
R_M = 6_371_008.8


def dest(lat, lon, brg_deg, dist_m):
    d, b = dist_m / R_M, math.radians(brg_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


def track(icao="abc123", n=60, step=10, lat=55.0, lon=20.0, hdg=90.0, speed=230.0, baro=10000.0,
          geo_off=40.0, t0=T0, turn=0.0, nic=8, nacp=9):
    """Clean straight (or gently turning) cruise; velocity/track agree with the positions."""
    out = []
    for i in range(n):
        out.append(StateVector(
            icao24=icao, callsign=icao.upper(), time=t0 + i * step, lat=lat, lon=lon,
            baro_alt_m=baro, geo_alt_m=None if geo_off is None else baro + geo_off,
            velocity_ms=speed, track_deg=hdg % 360, vertical_rate_ms=0.0, on_ground=False,
            nic=nic, nacp=nacp, position_source="adsb",
        ))
        lat, lon = dest(lat, lon, hdg, speed * step)
        hdg += turn
    return out


def snap(states, bbox=BBOX):
    states = sorted(states, key=lambda s: (s.icao24, s.time))
    return Snapshot(name="t", bbox=bbox, t_start=states[0].time, t_end=max(s.time for s in states),
                    states=states, kind="synthetic")


def edit(states, idx, **kw):
    """Copy of `states` with fields replaced on the given indices."""
    idx = set(idx)
    return [s.model_copy(update=kw) if i in idx else s for i, s in enumerate(states)]


def kinds(anoms):
    return {a.kind for a in anoms}


def of(anoms, kind):
    return [a for a in anoms if a.kind == kind]


# ---------------------------------------------------------------------------
# position_jump
# ---------------------------------------------------------------------------

def test_position_jump_positive_reports_last_plausible_fix():
    base = track()
    moved = [s.model_copy(update={"lat": s.lat + 0.4}) if i >= 20 else s for i, s in enumerate(base)]
    a = detect(snap(moved))
    jumps = of(a, "position_jump")
    assert len(jumps) == 1 and kinds(a) == {"position_jump"}
    j = jumps[0]
    assert j.time == base[20].time
    assert (j.lat, j.lon) == (base[19].lat, base[19].lon)  # BEFORE the jump, not the spoofed fix
    assert j.severity >= 0.8 and "km" in j.detail and "m/s" in j.detail


def test_position_jump_and_return_locates_both_edges_on_the_true_track():
    base = track()
    moved = [s.model_copy(update={"lat": s.lat + 0.4}) if 15 <= i < 25 else s for i, s in enumerate(base)]
    jumps = of(detect(snap(moved)), "position_jump")
    assert [j.time for j in jumps] == [base[15].time, base[25].time]
    assert (jumps[0].lat, jumps[0].lon) == (base[14].lat, base[14].lon)   # entry: fix before
    assert (jumps[1].lat, jumps[1].lon) == (base[25].lat, base[25].lon)   # exit: back on true track


def test_position_jump_negative_fast_flight_and_60s_gap():
    fast = track(speed=320.0)                       # strong tailwind, still legal
    gap = [s for i, s in enumerate(track("def456")) if not 10 <= i <= 14]   # 60 s of silence
    assert detect(snap(fast + gap)) == []


# ---------------------------------------------------------------------------
# impossible_speed
# ---------------------------------------------------------------------------

def test_impossible_speed_positive_high_and_slow_at_cruise():
    a = detect(snap(edit(track(), [10], velocity_ms=520.0)))
    fast = of(a, "impossible_speed")
    assert len(fast) == 1 and fast[0].time == T0 + 100 and "520" in fast[0].detail
    slow = detect(snap(edit(track("aaa111"), [5], velocity_ms=15.0)))
    assert [x.kind for x in of(slow, "impossible_speed")] == ["impossible_speed"]


def test_impossible_speed_negative_edge_cases():
    ok = edit(track(), [3], velocity_ms=340.0)                                # near, not over, the limit
    ground = edit(track("g00001"), range(60), on_ground=True, velocity_ms=5.0, baro_alt_m=0.0, geo_alt_m=40.0)
    heli = track("h00001", speed=30.0, baro=500.0)                            # slow but low: fine
    missing = edit(track("m00001"), [7], velocity_ms=None)
    a = detect(snap(ok + ground + heli + missing))
    assert of(a, "impossible_speed") == []


# ---------------------------------------------------------------------------
# baro_geo_mismatch
# ---------------------------------------------------------------------------

def test_baro_geo_mismatch_positive():
    st = edit(track(), range(10, 20), geo_alt_m=10900.0)                      # +900 m vs baro 10000
    bg = of(detect(snap(st)), "baro_geo_mismatch")
    assert len(bg) == 10 and all(a.severity > 0.3 for a in bg)
    assert bg[0].alt_m == 10000.0 and "GNSS altitude" in bg[0].detail


def test_baro_geo_mismatch_relative_to_own_offset():
    # natural +100 m offset is fine; a +350 m step (total 450, under the absolute limit) is caught
    st = edit(track(geo_off=100.0), range(30, 40), geo_alt_m=10450.0)
    assert {a.time for a in of(detect(snap(st)), "baro_geo_mismatch")} == {T0 + 10 * i for i in range(30, 40)}


def test_baro_geo_mismatch_negative_normal_offsets_and_missing_fields():
    natural = track("n00001", geo_off=-180.0)                                 # ordinary pressure/geoid offset
    no_geo = track("n00002", geo_off=None)
    no_baro = edit(track("n00003"), range(60), baro_alt_m=None)
    assert detect(snap(natural + no_geo + no_baro)) == []


def test_baro_geo_natural_offset_is_systematic_and_grows_with_altitude():
    """Real feeds (e.g. the shipped India sample) show +600..780 m at cruise, ~0 near the ground."""
    tropical = fleet(geo_off=650.0)                                            # level cruise, all +650
    climbers = []
    for k in range(6):
        base = track(f"cl{k:04d}", lat=54.0 + 0.3 * k)
        climbers += [s.model_copy(update={"baro_alt_m": 175.0 * i, "geo_alt_m": 175.0 * i * 1.065 + 5.0 * k,
                                          "vertical_rate_ms": 17.5}) for i, s in enumerate(base)]
    assert detect(snap(tropical + climbers)) == []


def test_baro_geo_regional_norm_flags_an_odd_aircraft_with_too_few_samples_of_its_own():
    peers = []
    for k in range(8):
        peers += track(f"p{k:05d}", n=3, lat=55.0 + 0.1 * k, baro=10800.0, geo_off=650.0)
    normal = track("norm01", n=3, baro=10800.0, geo_off=700.0)
    odd = track("odd001", n=3, baro=10800.0, geo_off=1000.0)                    # 350 m off the regional norm
    a = detect(snap(peers + normal + odd))
    assert {x.icao24 for x in a} == {"odd001"} and kinds(a) == {"baro_geo_mismatch"}
    assert "regional norm" in a[0].detail


def test_baro_geo_needs_adjacent_samples_and_forgives_climbs():
    assert of(detect(snap(edit(track(), [30], geo_alt_m=10900.0))), "baro_geo_mismatch") == []       # lone glitch
    assert len(of(detect(snap(edit(track(), [30, 31], geo_alt_m=10900.0))), "baro_geo_mismatch")) == 2
    assert len(of(detect(snap(edit(track(), [30, 31], geo_alt_m=10500.0, vertical_rate_ms=0.0))),
                  "baro_geo_mismatch")) == 2
    climbing = edit(track(), [30, 31], geo_alt_m=10500.0, vertical_rate_ms=15.0)     # stale-by-seconds sources
    assert of(detect(snap(climbing)), "baro_geo_mismatch") == []
    assert of(detect(snap(climbing), DetectConfig(baro_geo_lag_s=0.0)), "baro_geo_mismatch")     # control


# ---------------------------------------------------------------------------
# velocity_mismatch
# ---------------------------------------------------------------------------

def test_velocity_mismatch_positive_drift_like():
    # reports 230 m/s but the positions only advance at 100 m/s (offset growing backwards)
    st = edit(track(speed=100.0), range(60), velocity_ms=230.0)
    vm = of(detect(snap(st)), "velocity_mismatch")
    assert len(vm) == 59
    first = st[0]
    assert all((a.lat, a.lon) == (first.lat, first.lon) for a in vm)   # last fix before mismatch began
    assert "230" in vm[0].detail and "100" in vm[0].detail


def test_velocity_mismatch_negative_turns_gaps_missing_velocity():
    turning = track("t00001", turn=20.0)                                        # sharp turn each step
    # 3 deg/s turn sampled every 60 s: the chord is much shorter than the arc flown
    hold = edit(track("t00002", step=60, speed=63.7, turn=180.0), range(60), velocity_ms=100.0)
    gap = [s for i, s in enumerate(track("t00003")) if not 10 <= i <= 14]
    no_vel = edit(track("t00004"), range(60), velocity_ms=None, track_deg=None)
    assert of(detect(snap(turning + hold + gap + no_vel)), "velocity_mismatch") == []


# ---------------------------------------------------------------------------
# duplicate_id
# ---------------------------------------------------------------------------

def test_duplicate_id_positive():
    st = track()
    st.append(st[10].model_copy(update={"lat": st[10].lat + 1.35}))           # ~150 km away, same instant
    a = detect(snap(st))
    dup = of(a, "duplicate_id")
    assert len(dup) == 1 and dup[0].time == st[10].time and "150 km" in dup[0].detail
    # an ambiguous track is not also reported as a chain of position jumps
    assert of(a, "position_jump") == [] and of(a, "velocity_mismatch") == []


def test_duplicate_id_negative_same_instant_a_few_km_apart():
    st = track()
    st.append(st[10].model_copy(update={"lat": st[10].lat + 0.02}))           # ~2 km: two receivers
    assert detect(snap(st)) == []


# ---------------------------------------------------------------------------
# integrity_drop
# ---------------------------------------------------------------------------

def test_integrity_drop_positive_only_after_a_good_level():
    st = edit(track(), range(20, 60), nic=4)
    ig = of(detect(snap(st)), "integrity_drop")
    assert len(ig) == 40 and ig[0].time == T0 + 200
    assert max(a.severity for a in ig) <= 0.6            # integrity is a weak signal by design
    assert "NIC" in ig[0].detail


def test_integrity_drop_negative_absent_constant_or_never_good():
    absent = track("i00001", nic=None, nacp=None)
    good = track("i00002")
    zeros = track("i00003", nic=0, nacp=0)                                    # old transponders send 0
    lowfix = track("i00004", nic=5, nacp=5)                                   # low from the start: no drop
    assert detect(snap(absent + good + zeros + lowfix)) == []


# ---------------------------------------------------------------------------
# signal_dropout
# ---------------------------------------------------------------------------

def fleet(lon0=20.0, **kw):
    """Five aircraft flying near each other, 600 s, inside the bbox."""
    out = []
    for k, name in enumerate("abcde"):
        out += track(f"{name}00000", n=61, lat=55.0 + 0.2 * k, lon=lon0 + 0.1 * k, **kw)
    return out


def silence(states, names, lo, hi):
    """Remove samples lo <= index < hi (by time order) of aircraft "<letter>00000"."""
    gone = set()
    for n in names:
        mine = sorted((s for s in states if s.icao24 == f"{n}00000"), key=lambda s: s.time)
        gone |= {id(s) for s in mine[lo:hi]}
    return [s for s in states if id(s) not in gone]


def test_signal_dropout_positive_cluster_goes_silent_while_others_report():
    st = silence(fleet(), "abc", 20, 45)
    a = detect(snap(st))
    assert kinds(a) == {"signal_dropout"} and {x.icao24 for x in a} == {"a00000", "b00000", "c00000"}
    d = next(x for x in a if x.icao24 == "a00000")
    assert d.time == T0 + 190 and "2 nearby aircraft" in d.detail and "2 others" in d.detail
    assert d.severity <= 0.8
    last = next(s for s in st if s.icao24 == "a00000" and s.time == d.time)
    assert (d.lat, d.lon) == (last.lat, last.lon)        # last known fix before silence


def test_signal_dropout_positive_tail_never_resumes():
    a = detect(snap(silence(fleet(), "abc", 30, 61)))
    assert {x.icao24 for x in a} == {"a00000", "b00000", "c00000"}
    assert all("never resumed" in x.detail for x in a)


def test_signal_dropout_negative_each_guard_and_its_control():
    def flagged(states, cfg=None, bbox=BBOX):
        return of(detect(snap(states, bbox=bbox), cfg), "signal_dropout")

    lone, pair = silence(fleet(), "a", 20, 45), silence(fleet(), "ab", 20, 45)
    assert flagged(lone) == [] and flagged(pair) == []                           # coverage gap, not an event
    assert flagged(lone, DetectConfig(dropout_min_cluster=1))                    # control

    outage = silence(fleet(), "abcde", 20, 45)                                   # whole feed down
    assert flagged(outage) == []

    short = silence(fleet(), "abc", 20, 25)                                      # 60 s: normal reporting gap
    assert flagged(short) == []
    assert flagged(short, DetectConfig(dropout_gap_s=30.0, dropout_cadence_factor=1.0))

    low = silence(fleet(baro=1500.0), "abc", 20, 45)                             # low aircraft leave coverage
    assert flagged(low) == []
    assert flagged(low, DetectConfig(dropout_min_alt_m=0.0))

    grounded = [s.model_copy(update={"on_ground": True}) if s.icao24[0] in "abc" else s for s in fleet()]
    assert flagged(silence(grounded, "abc", 20, 45)) == []

    edge_box = (45.0, 5.0, 65.0, 21.0)                                           # they left the area
    edge = silence(fleet(lon0=20.5), "abc", 5, 45)
    assert flagged(edge, bbox=edge_box) == []
    assert flagged(edge, DetectConfig(dropout_edge_km=0.0), bbox=edge_box)

    brief = [s for s in fleet() if not (s.icao24[0] in "abc" and s.time >= T0 + 20)]   # only 2 reports, then gone
    assert flagged(brief) == []
    assert flagged(brief, DetectConfig(dropout_min_history=1))


# ---------------------------------------------------------------------------
# long clean flights: nothing may fire
# ---------------------------------------------------------------------------

def realistic_flight(icao, seed, n=1080, step=10, lat=52.0, lon=8.0, hdg=70.0):
    """3 h of ordinary traffic: taxi, climb, cruise with gentle turns, GPS noise, a natural
    baro/GNSS offset, missing fields (no NIC/NACp) and report gaps of up to 60 s."""
    rng = np.random.default_rng(seed)
    geo_off = float(rng.uniform(-250.0, 250.0))
    speeds = np.where(np.arange(n) < 12, 8.0, np.minimum(230.0, 80.0 + 150.0 * (np.arange(n) - 12) / 60.0))
    baro = np.where(np.arange(n) < 12, 0.0, np.minimum(10500.0, 10500.0 * (np.arange(n) - 12) / 60.0))
    out, h = [], hdg
    for i in range(n):
        if i >= 72 and i % 180 < 5:
            h += 6.0                                  # 30 deg course change over 50 s
        elif i >= 72:
            h += 0.05
        nlat, nlon = lat + rng.normal(0, 8) / 111195.0, lon + rng.normal(0, 8) / (111195.0 * math.cos(math.radians(lat)))
        out.append(StateVector(
            icao24=icao, callsign=icao.upper(), time=T0 + i * step, lat=nlat, lon=nlon,
            baro_alt_m=float(baro[i]), geo_alt_m=None if i % 7 == 0 else float(baro[i] * 1.06 + geo_off),
            velocity_ms=None if i % 11 == 0 else float(speeds[i]),
            track_deg=None if i % 11 == 0 else h % 360,
            vertical_rate_ms=None if i % 13 == 0 else (17.5 if 12 <= i < 72 else 0.0),
            on_ground=i < 12, position_source="adsb",
        ))
        v_mid = (speeds[i] + speeds[min(i + 1, n - 1)]) / 2
        lat, lon = dest(lat, lon, h, v_mid * step)
    for j in range(100, n - 5, 97):                   # 60 s reporting gaps
        for k in range(j, j + 5):
            out[k] = None
    return [s for s in out if s is not None]


def test_long_clean_tracks_produce_zero_anomalies():
    st = []
    for k, name in enumerate(("c00001", "c00002", "c00003")):
        st += realistic_flight(name, seed=k, lat=52.0 + k, lon=8.0 + 2 * k)
    assert len(st) > 3000
    assert detect(snap(st, bbox=(30.0, -20.0, 80.0, 60.0))) == []


# ---------------------------------------------------------------------------
# score_flights
# ---------------------------------------------------------------------------

def A(icao, kind, sev, lat=55.0, lon=20.0, t=T0):
    return Anomaly(icao24=icao, time=t, lat=lat, lon=lon, kind=kind, severity=sev, detail="x")


def test_score_flights_bounds_ordering_and_evidence():
    assert score_flights([]) == {}
    an = [A("aa", "position_jump", 0.9), A("aa", "position_jump", 0.9, t=T0 + 10),
          A("aa", "baro_geo_mismatch", 0.8), A("bb", "integrity_drop", 0.6), A("cc", "signal_dropout", 0.5)]
    sc = score_flights(an)
    assert set(sc) == {"aa", "bb", "cc"} and all(0.0 <= v <= 1.0 for v in sc.values())
    assert sc["aa"] > sc["cc"] > sc["bb"]                              # jump+altitude beats a lone weak signal
    assert score_flights(an + [A("bb", "position_jump", 0.9)])["bb"] > sc["bb"]   # more evidence, higher score
    assert score_flights([A("zz", k, 1.0, t=T0 + i) for k in ("position_jump", "duplicate_id", "baro_geo_mismatch")
                          for i in range(9)])["zz"] <= 1.0


def test_score_flights_from_detection_flags_only_the_spoofed_aircraft():
    st = track("bad001")
    st = [s.model_copy(update={"lat": s.lat + 0.4, "geo_alt_m": s.geo_alt_m + 900}) if 20 <= i < 40 else s
          for i, s in enumerate(st)] + track("ok0001", lat=56.0)
    sc = score_flights(detect(snap(st)))
    assert set(sc) == {"bad001"} and sc["bad001"] >= 0.8


# ---------------------------------------------------------------------------
# region_summary
# ---------------------------------------------------------------------------

def test_region_summary_bins_counts_and_severity():
    an = [A("a", "position_jump", 0.5, 55.2, 20.3), A("b", "baro_geo_mismatch", 0.9, 55.7, 20.9),
          A("a", "velocity_mismatch", 0.4, 55.9, 20.1), A("c", "signal_dropout", 0.3, 57.1, 23.6)]
    cells = region_summary(an)
    assert [(c.lat, c.lon, c.n_anomalies, c.n_aircraft, c.max_severity) for c in cells] == [
        (55.5, 20.5, 3, 2, 0.9), (57.5, 23.5, 1, 1, 0.3)]
    fine = region_summary(an, cell_deg=0.5)
    assert len(fine) == 4 and sum(c.n_anomalies for c in fine) == 4
    neg = region_summary([A("a", "integrity_drop", 0.4, -0.2, -0.3)])[0]
    assert (neg.lat, neg.lon) == (-0.5, -0.5)                           # floor, not truncation


def test_region_summary_empty_and_bad_cell():
    assert region_summary([]) == []
    with pytest.raises(ValueError):
        region_summary([A("a", "integrity_drop", 0.4)], cell_deg=0)


# ---------------------------------------------------------------------------
# config, robustness, speed
# ---------------------------------------------------------------------------

def test_config_thresholds_are_honoured():
    jumped = snap([s.model_copy(update={"lat": s.lat + 0.4}) if i >= 20 else s for i, s in enumerate(track())])
    assert of(detect(jumped), "position_jump")
    assert of(detect(jumped, DetectConfig(jump_speed_ms=1e6)), "position_jump") == []
    high_offset = snap(track(geo_off=300.0))
    assert detect(high_offset) == []
    assert len(of(detect(high_offset, DetectConfig(baro_geo_abs_m=200.0)), "baro_geo_mismatch")) == 60
    assert of(detect(snap(edit(track(), [10], velocity_ms=520.0)), DetectConfig(max_speed_ms=600.0)),
              "impossible_speed") == []


def test_input_order_and_bad_values_do_not_matter():
    st = [s.model_copy(update={"lat": s.lat + 0.4}) if 15 <= i < 25 else s for i, s in enumerate(track())] + fleet()
    ordered = snap(st)
    shuffled = ordered.model_copy(update={"states": list(np.random.default_rng(0).permutation(ordered.states))})
    before = ordered.model_dump()
    a, b = detect(ordered), detect(shuffled)
    assert a and [x.model_dump() for x in a] == [x.model_dump() for x in b]
    assert ordered.model_dump() == before                               # input untouched
    empty = Snapshot(name="e", bbox=BBOX, t_start=T0, t_end=T0, states=[])
    assert detect(empty) == [] and detect(snap(track(n=1))) == []
    assert detect(snap(edit(track(), [5], lat=float("nan")))) == []      # garbage fix is ignored, no crash


def test_100k_states_in_about_two_seconds_and_finds_the_injected_aircraft():
    rng = np.random.default_rng(0)
    n_ac, n_steps, step = 2000, 50, 10
    lat0, lon0, v = rng.uniform(45, 60, n_ac), rng.uniform(5, 30, n_ac), rng.uniform(200, 250, n_ac)
    hit = set(range(0, n_ac, 20))
    states = []
    for a in range(n_ac):
        baro = 9000.0 + 300.0 * (a % 10)
        for i in range(n_steps):
            lat = lat0[a] + (0.4 if a in hit and i >= 20 else 0.0)
            lon = lon0[a] + v[a] * step * i / (111195.0 * math.cos(math.radians(lat0[a])))
            states.append(StateVector(icao24=f"{a:06x}", time=T0 + i * step, lat=float(lat), lon=float(lon),
                                      baro_alt_m=baro, geo_alt_m=baro + 40.0, velocity_ms=float(v[a]),
                                      track_deg=90.0, on_ground=False))
    s = snap(states)
    assert len(s.states) == 100_000
    t = time.perf_counter()
    an = detect(s)
    assert time.perf_counter() - t < 2.0
    assert {a.icao24 for a in an} == {f"{a:06x}" for a in hit}


# ---------------------------------------------------------------------------
# P1 integration: runs only when gnsswatch.data / simulate exist
# ---------------------------------------------------------------------------

P1_BBOX = (54.0, 19.0, 61.0, 28.0)
SRC = (57.5, 23.5)


def _p1():
    return pytest.importorskip("gnsswatch.data"), pytest.importorskip("gnsswatch.simulate")


def _hav(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def _precision_recall(anoms, truth):
    flagged = {a.icao24 for a in anoms}
    tp = len(flagged & truth)
    return (tp / len(flagged) if flagged else 1.0), (tp / len(truth) if truth else 1.0)


def test_p1_clean_baselines_have_no_anomalies():
    data, _ = _p1()
    for seed in range(3):
        assert detect(data.synthetic_baseline(P1_BBOX, seed=seed)) == []


@pytest.mark.parametrize("mode", ["teleport", "drift", "attractor"])
def test_p1_injected_spoof_is_caught(mode):
    data, sim = _p1()
    base = data.synthetic_baseline(P1_BBOX, seed=0)
    s = sim.inject_spoof(base, SRC, 150.0, base.t_start + 400, base.t_start + 1400, mode=mode, seed=1)
    truth = set(s.injected.affected_icao24)
    assert truth and s.kind == "synthetic"
    precision, recall = _precision_recall(detect(s), truth)
    assert precision == 1.0 and recall >= 0.9


def test_p1_injected_jam_is_caught():
    data, sim = _p1()
    base = data.synthetic_baseline(P1_BBOX, seed=0)
    s = sim.inject_jam(base, SRC, 150.0, base.t_start + 400, base.t_start + 1400, seed=1)
    truth = set(s.injected.affected_icao24)
    an = detect(s)
    precision, recall = _precision_recall(an, truth)
    assert kinds(an) == {"signal_dropout"} and precision == 1.0 and recall >= 0.9


def test_p1_jump_locations_are_true_positions_near_the_source():
    data, sim = _p1()
    base = data.synthetic_baseline(P1_BBOX, seed=0)
    s = sim.inject_spoof(base, SRC, 150.0, base.t_start + 400, base.t_start + 1400, mode="teleport", seed=1)
    jumps = of(detect(s), "position_jump")
    assert jumps
    assert all(_hav((j.lat, j.lon), SRC) <= 150.0 + 5.0 for j in jumps)   # spoofed fixes are 20-60 km off


def test_shipped_real_sample_stays_quiet():
    """Precision guard on real traffic. NOTE: baro_geo and dropout thresholds were adjusted after
    looking at this sample, so it is a regression guard, not independent validation."""
    data = pytest.importorskip("gnsswatch.data")
    if "live_india_sample" not in data.list_snapshots():
        pytest.skip("live_india_sample not shipped")
    s = data.load_snapshot("live_india_sample")
    assert s.kind == "real" and detect(s) == []
