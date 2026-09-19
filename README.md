# GNSS Watch

An MCP server for airline flight-ops and safety teams. It flags GNSS (GPS) spoofing and jamming in public ADS-B data, estimates where the interference is coming from, and warns before takeoff with a route risk and a pre-flight brief.

**We cannot stop someone broadcasting a fake signal. We stop it from mattering: verify, locate, warn.**

- **Verify** positions with cross-checks that don't trust GNSS: barometric vs GNSS altitude, reported vs implied speed, position jumps, duplicate IDs, dropouts. Integrity flags are one signal only, because spoofing can leave them looking normal.
- **Locate** the likely interference source from where the affected aircraft are.
- **Warn** before departure with a route risk score and a pre-flight evidence pack.

**Why MCP:** detection is deterministic code exposed as tools. The AI assistant only chooses tools and explains results, so it cannot invent alerts. Any MCP-capable assistant can use it, with no new app to log into.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
python -m pytest
python demo/check_end_to_end.py
```

Register with Claude Code (from the repo root, venv active):

```bash
claude mcp add gnss-watch -- python -m gnsswatch.server
```

If `python` isn't the venv's, give the full path: `claude mcp add gnss-watch -- $PWD/.venv/bin/python -m gnsswatch.server`.

## Tools

| Tool | What it does |
|---|---|
| `list_snapshots` | Saved ADS-B snapshots (real or synthetic) |
| `analyze_snapshot(name)` | Anomalies, top flagged aircraft, hot cells, source estimate |
| `inject_demo_event(base, mode, source_lat, source_lon, radius_km, name)` | SIMULATED spoof (`teleport`/`drift`/`attractor`) or jam (`dropout`) |
| `route_check(origin, destination, hour_utc, snapshot)` | Route risk 0..1 from a snapshot (observed) or the illustrative seed |
| `preflight_brief(origin, destination, hour_utc, snapshot)` | Evidence pack + deterministic fallback text |
| `get_weather(icao)` | METAR from aviationweather.gov (sample fallback) |
| `get_notams(icao)` | FAA NOTAMs if `FAA_CLIENT_ID`/`FAA_CLIENT_SECRET` are set, else a labelled SAMPLE |

Environment: `GNSSWATCH_OFFLINE=1` skips all network calls; `GNSSWATCH_STUBS=1` forces the built-in stubs.

## Honest limits

- Public ADS-B data makes this **situational awareness, not a certified operational tool**.
- The demo spoofing event is **SIMULATED**, and every output that touches it carries `is_synthetic: true`. Seed hot-zone data is **ILLUSTRATIVE, not measured**. Neither says anything about real-world accuracy.
- Passive data only: nothing is transmitted and no airline or airport systems are probed.
- The long-term fix is authenticated signals. Galileo OSNMA has been operational since 24 July 2025. It lets receivers identify unauthenticated navigation data, but it doesn't prevent spoofing or jamming, and it only helps once receivers implement it. Aviation standards are still being finalised. Until then there is a gap, and GNSS Watch covers it.

## Layout

`gnsswatch/models.py` holds the shared models (frozen). `data.py` and `simulate.py` are P1's, `detect.py` P2's, and `locate.py`, `hotzones.py` and `airports.py` P3's. The server, briefs and extras are P4's. `stubs.py` stands in for any module that isn't merged yet. See `CONTRACT.md`.
