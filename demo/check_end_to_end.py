"""End-to-end check: clean baseline -> SIMULATED spoof -> detect -> locate -> route risk.

Run from the repo root: python demo/check_end_to_end.py   (exit 0 = pass)
Uses real modules where merged, stubs otherwise (see "backends" line).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("GNSSWATCH_OFFLINE", "1")

from gnsswatch import server as S  # noqa: E402

failures = []


def check(ok: bool, msg: str) -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {msg}")
    if not ok:
        failures.append(msg)


real = sorted({v for v in S.BACKENDS.values() if v != "stub"})
print(f"backends: real={real or 'none'} stubs={sorted(k for k, v in S.BACKENDS.items() if v == 'stub')}")

base = S._load(S.DEMO_BASE)
lat0, lon0, lat1, lon1 = base.bbox
src = ((lat0 + lat1) / 2, (lon0 + lon1) / 2)
route = (f"{lat0 + 0.2:.2f},{src[1]:.2f}", f"{lat1 - 0.2:.2f},{src[1]:.2f}")  # S->N through the source

clean = S.analyze_snapshot(S.DEMO_BASE)
check(clean["counts"]["n_anomalies"] == 0, f"clean baseline: {clean['counts']['n_anomalies']} anomalies (want 0)")

ev = S.inject_demo_event(S.DEMO_BASE, "teleport", src[0], src[1], 150.0, "e2e_spoof")
print(f"injected SIMULATED teleport spoof at {src[0]:.2f},{src[1]:.2f} r=150 km, "
      f"{ev['n_affected_aircraft']} aircraft affected")

res = S.analyze_snapshot("e2e_spoof")
sc = res["simulation_check"]
check(res["counts"]["n_anomalies"] > 0, f"spoof detected: {res['counts']['by_kind']}")
check(sc["affected_and_flagged"] > 0,
      f"flagged {sc['affected_and_flagged']}/{sc['affected_aircraft']} affected aircraft, "
      f"{sc['flagged_not_affected']} false positives")
se = res["source_estimate"]
check(se is not None, "source estimated")
if se:
    err = sc["source_error_km"]
    print(f"source estimate {se['lat']:.2f},{se['lon']:.2f} +/-{se['radius_km']:.0f} km "
          f"conf {se['confidence']:.2f} ({se['method']})")
    check(err < 150, f"source error vs ground truth: {err} km (want < 150)")

before = S.route_check(*route, snapshot=S.DEMO_BASE)
after = S.route_check(*route, snapshot="e2e_spoof")
print(f"route {route[0]} -> {route[1]}: baseline {before['risk_level']} {before['score']:.2f}, "
      f"spoofed {after['risk_level']} {after['score']:.2f}")
check(after["score"] > before["score"], "route risk rises after the SIMULATED spoof")

print("\nALL PASS" if not failures else f"\n{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
