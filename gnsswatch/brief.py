"""Evidence packs: facts only, JSON-friendly. The assistant writes the polished brief from these;
fallback_text() gives a deterministic plain-text brief if no assistant is available."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from gnsswatch.models import Anomaly, RegionCell, RouteRisk, Snapshot, SourceEstimate

BASE_CAVEATS = [
    "Situational awareness from public ADS-B data; not a certified operational tool.",
    "Integrity flags (NIC/NACp) are one signal only; spoofing can leave them looking normal.",
]
SIM_CAVEAT = "SIMULATED data: this snapshot is synthetic and any event in it was injected for demo purposes."
SEED_CAVEAT = "Hot-zone seed data is ILLUSTRATIVE, not measured."
BRIEF_INSTRUCTIONS = ("Write a short pre-flight brief from these facts only. Do not add incidents, numbers or "
                      "causes that are not in this pack. Keep every caveat.")


def risk_level(score: float) -> str:
    return "HIGH" if score >= 0.5 else "MODERATE" if score >= 0.2 else "LOW"


def _utc(t: int) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def is_synthetic(s: Snapshot) -> bool:
    return s.kind == "synthetic"


def analysis_pack(s: Snapshot, anoms: list[Anomaly], scores: dict[str, float],
                  cells: list[RegionCell], source: SourceEstimate | None, top_n: int = 5) -> dict:
    callsigns = {st.icao24: st.callsign for st in s.states if st.callsign}
    kinds_by_id: dict[str, Counter] = {}
    for a in anoms:
        kinds_by_id.setdefault(a.icao24, Counter())[a.kind] += 1
    top = sorted(scores.items(), key=lambda kv: -kv[1])[:top_n]
    flagged = [{"icao24": i, "callsign": callsigns.get(i), "score": round(sc, 2),
                "anomaly_kinds": dict(kinds_by_id.get(i, {}))} for i, sc in top]
    hot = sorted(cells, key=lambda c: (-c.max_severity, -c.n_anomalies))[:top_n]
    return {
        "snapshot": s.name,
        "is_synthetic": is_synthetic(s),
        "window_utc": [_utc(s.t_start), _utc(s.t_end)],
        "counts": {
            "n_states": len(s.states),
            "n_aircraft": len({st.icao24 for st in s.states}),
            "n_anomalies": len(anoms),
            "n_flagged_aircraft": len(scores),
            "by_kind": dict(Counter(a.kind for a in anoms)),
        },
        "top_flagged_aircraft": flagged,
        "hot_cells": [c.model_dump() for c in hot],
        "source_estimate": source.model_dump() if source else None,
        "caveats": BASE_CAVEATS + ([SIM_CAVEAT] if is_synthetic(s) else []),
    }


def route_pack(risk: RouteRisk, synthetic: bool, snapshot: str | None) -> dict:
    caveats = list(BASE_CAVEATS)
    if risk.basis == "illustrative_seed":
        caveats.append(SEED_CAVEAT)
    if synthetic:
        caveats.append(SIM_CAVEAT)
    return {**risk.model_dump(), "risk_level": risk_level(risk.score), "snapshot": snapshot,
            "is_synthetic": synthetic, "caveats": caveats}


def preflight_pack(route: dict, analysis: dict | None, weather: list[dict], notams: list[dict]) -> dict:
    caveats = list(dict.fromkeys(route["caveats"] + (analysis["caveats"] if analysis else [])))
    if any(n.get("is_sample") for n in notams):
        caveats.append("NOTAMs shown are SAMPLES, not real NOTAMs.")
    if any(w.get("live") is False for w in weather):
        caveats.append("Some weather is a cached or sample METAR, not current.")
    pack = {
        "instructions": BRIEF_INSTRUCTIONS,
        "is_synthetic": route["is_synthetic"] or bool(analysis and analysis["is_synthetic"]),
        "route": {k: route[k] for k in ("origin", "destination", "hour_utc", "score", "risk_level",
                                        "basis", "cells", "advice")},
        "live_picture": None if analysis is None else {
            k: analysis[k] for k in ("snapshot", "window_utc", "counts", "top_flagged_aircraft",
                                     "source_estimate")},
        "weather": weather,
        "notams": notams,
        "caveats": caveats,
    }
    pack["fallback_text"] = fallback_text(pack)
    return pack


def fallback_text(pack: dict) -> str:
    r = pack["route"]
    lines = [f"GNSS WATCH PRE-FLIGHT BRIEF  {r['origin']} -> {r['destination']}  (hour {r['hour_utc']:02d} UTC)"]
    if pack["is_synthetic"]:
        lines.append("*** SIMULATED DATA: DEMO ONLY ***")
    lines.append(f"Route GNSS interference risk: {r['risk_level']} (score {r['score']:.2f}, "
                 f"basis: {r['basis']}), {len(r['cells'])} flagged cell(s) in corridor.")
    lp = pack["live_picture"]
    if lp:
        c = lp["counts"]
        lines.append(f"Snapshot {lp['snapshot']}: {c['n_anomalies']} anomalies on "
                     f"{c['n_flagged_aircraft']}/{c['n_aircraft']} aircraft {c['by_kind']}.")
        se = lp["source_estimate"]
        if se:
            lines.append(f"Likely interference area: {se['lat']:.2f}, {se['lon']:.2f} "
                         f"+/- {se['radius_km']:.0f} km (confidence {se['confidence']:.2f}).")
    for w in pack["weather"]:
        if w.get("raw"):
            lines.append(f"METAR{'' if w.get('live') else ' (not live)'}: {w['raw']}")
    for n in pack["notams"]:
        g = [x for x in n.get("notams", []) if x.get("gnss_related")]
        if g:
            lines.append(f"{n['icao']}: {len(g)} GNSS-related NOTAM(s){' [SAMPLE]' if n.get('is_sample') else ''}.")
    lines.append("Actions:")
    lines += [f"- {a}" for a in r["advice"]]
    lines.append("Caveats:")
    lines += [f"- {c}" for c in pack["caveats"]]
    return "\n".join(lines)
