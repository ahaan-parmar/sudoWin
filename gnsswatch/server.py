"""GNSS Watch MCP server (stdio). Run: python -m gnsswatch.server

Detection is deterministic code in these tools; the assistant chooses tools and explains results.
Each backend function comes from the real module (data, simulate, detect, locate, hotzones) when
present, else from gnsswatch.stubs. Set GNSSWATCH_STUBS=1 to force stubs.
"""
from __future__ import annotations

import importlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from gnsswatch import brief, extras, stubs
from gnsswatch.models import Snapshot

BACKENDS: dict[str, str] = {}


def _pick(module: str, *names: str):
    mod = None
    if os.environ.get("GNSSWATCH_STUBS") != "1":
        try:
            mod = importlib.import_module(f"gnsswatch.{module}")
        except ImportError as e:
            if getattr(e, "name", None) != f"gnsswatch.{module}":
                print(f"gnss-watch: gnsswatch.{module} failed to import ({e}); using stubs", file=sys.stderr)
    fns = []
    for n in names:
        fn = getattr(mod, n, None)
        BACKENDS[n] = f"gnsswatch.{module}" if fn else "stub"
        fns.append(fn or getattr(stubs, n))
    return fns if len(fns) > 1 else fns[0]


load_snapshot, save_snapshot, list_snapshot_names, synthetic_baseline = _pick(
    "data", "load_snapshot", "save_snapshot", "list_snapshots", "synthetic_baseline")
inject_spoof, inject_jam = _pick("simulate", "inject_spoof", "inject_jam")
detect, score_flights, region_summary = _pick("detect", "detect", "score_flights", "region_summary")
estimate_source, haversine_km = _pick("locate", "estimate_source", "haversine_km")
build_hotzones, load_hotzones, route_risk = _pick("hotzones", "build_hotzones", "load_hotzones", "route_risk")

DEMO_BASE = "baseline"
DEMO_BBOX = (54.0, 19.0, 61.0, 28.0)  # Baltic region; used only if no baseline snapshot exists
SPOOF_MODES = ("teleport", "drift", "attractor")
MODES = SPOOF_MODES + ("dropout",)

mcp = FastMCP(
    "gnss-watch",
    instructions=(
        "GNSS Watch: detect GNSS spoofing/jamming in ADS-B snapshots, estimate the interference source, "
        "and assess route risk before departure. Typical flow: list_snapshots -> analyze_snapshot -> "
        "route_check or preflight_brief. Only report what tools return; never invent alerts or numbers. "
        "Anything with is_synthetic=true is SIMULATED and must be labelled so. Seed hot-zone data is "
        "illustrative, not measured. This is situational awareness, not a certified operational tool."
    ),
)


def _load(name: str) -> Snapshot:
    try:
        return load_snapshot(name)
    except FileNotFoundError:
        if name != DEMO_BASE:
            raise FileNotFoundError(f"no snapshot {name!r}; available: {list_snapshot_names()}")
        s = synthetic_baseline(DEMO_BBOX, name=DEMO_BASE)
        save_snapshot(s)
        return s


def _hour(t: int) -> int:
    return datetime.fromtimestamp(t, timezone.utc).hour


def _is_icao(code: str) -> bool:
    return len(code.strip()) == 4 and code.strip().isalnum()


@mcp.tool()
def list_snapshots() -> dict:
    """List saved ADS-B snapshots (real or SIMULATED) with size, time window and whether an event was
    injected. Call this first to see which snapshot names analyze_snapshot/route_check can use."""
    _load(DEMO_BASE)
    out = []
    for n in list_snapshot_names():
        try:
            s = _load(n)
        except Exception as e:
            out.append({"name": n, "error": str(e)})
            continue
        out.append({"name": s.name, "kind": s.kind, "is_synthetic": brief.is_synthetic(s),
                    "n_aircraft": len({st.icao24 for st in s.states}), "n_states": len(s.states),
                    "window_utc": [brief._utc(s.t_start), brief._utc(s.t_end)],
                    "injected_event": s.injected.mode if s.injected else None})
    return {"snapshots": out, "backends": BACKENDS}


@mcp.tool()
def analyze_snapshot(name: str) -> dict:
    """Run the deterministic spoofing/jamming detector on a snapshot. Returns anomaly counts by kind,
    the most suspicious aircraft, hot grid cells and an estimated interference source (lat, lon,
    uncertainty radius, confidence). Checks do not trust GNSS: baro vs GNSS altitude, implied vs
    reported speed, position jumps, duplicate IDs and signal dropouts. For SIMULATED snapshots it also
    scores the estimate against the injected ground truth (simulation_check)."""
    s = _load(name)
    anoms = detect(s)
    scores = score_flights(anoms)
    src = estimate_source(anoms)
    pack = brief.analysis_pack(s, anoms, scores, region_summary(anoms), src)
    if s.injected:
        ev = s.injected
        truth = set(ev.affected_icao24)
        err = haversine_km((src.lat, src.lon), (ev.source_lat, ev.source_lon)) if src else None
        pack["simulation_check"] = {
            "label": "Ground truth from the SIMULATED injection; used to score the detector only.",
            "mode": ev.mode, "true_source": [ev.source_lat, ev.source_lon], "true_radius_km": ev.radius_km,
            "affected_aircraft": len(truth), "affected_and_flagged": len(truth & set(scores)),
            "flagged_not_affected": len(set(scores) - truth),
            "source_error_km": None if err is None else round(err, 1),
        }
    return pack


@mcp.tool()
def inject_demo_event(base: str = DEMO_BASE, mode: str = "teleport", source_lat: float | None = None,
                      source_lon: float | None = None, radius_km: float = 150.0,
                      name: str | None = None) -> dict:
    """Create a SIMULATED interference event in a copy of a snapshot, for demos and testing only.
    mode: teleport | drift | attractor (spoofing) or dropout (jamming). The source defaults to the
    centre of the base snapshot's area; the event covers the middle third of its time window.
    The new snapshot is saved under `name` (default demo_<mode>) and is always marked synthetic.
    Follow with analyze_snapshot(name) or route_check(..., snapshot=name)."""
    if mode not in MODES:
        return {"error": f"mode must be one of {list(MODES)}"}
    s = _load(base)
    lat0, lon0, lat1, lon1 = s.bbox
    src = (source_lat if source_lat is not None else (lat0 + lat1) / 2,
           source_lon if source_lon is not None else (lon0 + lon1) / 2)
    third = (s.t_end - s.t_start) // 3
    t0, t1 = s.t_start + third, s.t_start + 2 * third
    name = name or f"demo_{mode}"
    if mode == "dropout":
        out = inject_jam(s, src, radius_km, t0, t1, name=name)
    else:
        out = inject_spoof(s, src, radius_km, t0, t1, mode=mode, name=name)
    path = save_snapshot(out)
    return {"name": out.name, "is_synthetic": True, "label": "SIMULATED event for demo purposes, not real data.",
            "base": base, "mode": mode, "source": list(src), "radius_km": radius_km,
            "window_utc": [brief._utc(t0), brief._utc(t1)],
            "n_affected_aircraft": len(out.injected.affected_icao24) if out.injected else None,
            "saved_to": path}


@mcp.tool()
def route_check(origin: str, destination: str, hour_utc: int | None = None,
                snapshot: str | None = None) -> dict:
    """Score GNSS interference risk (0..1, LOW/MODERATE/HIGH) along the corridor between two airports.
    origin/destination: ICAO code (e.g. EVRA) or "lat,lon". Without `snapshot`, uses the ILLUSTRATIVE
    hot-zone seed for hour_utc (default: current hour). With `snapshot`, uses what the detector finds
    in that snapshot right now ("is the route affected right now?"), at the snapshot's own hour.
    Returns the flagged cells crossed and short deterministic advice."""
    if snapshot:
        s = _load(snapshot)
        hz, hour, synthetic = build_hotzones(detect(s)), _hour(s.t_start), brief.is_synthetic(s)
    else:
        hz, synthetic = load_hotzones(), False
        hour = hour_utc if hour_utc is not None else datetime.now(timezone.utc).hour
    risk = route_risk(origin, destination, hour, hz)
    if snapshot:
        risk = risk.model_copy(update={"basis": "observed"})
    return brief.route_pack(risk, synthetic, snapshot)


@mcp.tool()
def preflight_brief(origin: str, destination: str, hour_utc: int | None = None,
                    snapshot: str | None = None) -> dict:
    """Build a pre-flight GNSS evidence pack for a route: route risk, the live picture from `snapshot`
    (if given), METARs and NOTAMs for ICAO endpoints, advice and caveats, plus a deterministic
    fallback_text. Write the crew brief from these facts only and keep every caveat."""
    route = route_check(origin, destination, hour_utc, snapshot)
    analysis = analyze_snapshot(snapshot) if snapshot else None
    icaos = [c.strip().upper() for c in (origin, destination) if _is_icao(c)]
    return brief.preflight_pack(route, analysis, [extras.get_weather(c) for c in icaos],
                                [extras.get_notams(c) for c in icaos])


@mcp.tool()
def get_weather(icao: str) -> dict:
    """Latest METAR for an airport (ICAO code) from aviationweather.gov. Falls back to a cached or
    clearly labelled sample METAR if the network is unavailable (live=false)."""
    return extras.get_weather(icao)


@mcp.tool()
def get_notams(icao: str) -> dict:
    """NOTAMs for an airport (ICAO code), GNSS-related ones first. Returns a clearly labelled SAMPLE
    unless FAA_CLIENT_ID and FAA_CLIENT_SECRET are set."""
    return extras.get_notams(icao)


def main() -> None:
    os.chdir(Path(__file__).resolve().parents[1])  # relative data/ paths resolve from the repo root
    mcp.run("stdio")


if __name__ == "__main__":
    main()
