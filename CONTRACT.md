# GNSS Watch: Shared Contract

Read this fully before writing code. It is the single source of truth for interfaces. Four people build four modules in parallel on separate branches, then merge. Any deviation from the signatures below breaks the merge.

## Product

An MCP server that helps airline flight-ops teams spot GNSS (GPS) spoofing/jamming from public ADS-B data, estimate where it's coming from, and warn before a flight leaves. Detection is deterministic code. An AI assistant (Claude) calls the tools and writes the plain-English brief, so it explains results but cannot invent alerts.

Built for AI-SEC Hackfest 2026, Track 03 (MCP & Agentic AI), in about 6 hours.

Demo: ask "is the route from A to B affected right now?" and get a brief. Then inject a SIMULATED spoofing event into a saved snapshot, and the answer changes, with a source estimate.

Honest framing: we cannot stop a fake signal. We stop it from mattering by verifying positions independently, pinpointing the source, and warning crews early. This is situational awareness from public data, not a certified operational tool.

## Rules

- Python 3.11+. Allowed deps: pydantic, numpy, httpx, pytest, and `mcp` (official Python SDK, server only). No ML training, no heavy deps.
- Each person works on their own branch and edits ONLY their own files (table below). `gnsswatch/models.py` is frozen. If you need a change, write it in `NOTES_<yourname>.md` and work around it.
- Run tests with `python -m pytest` from the repo root. Each module ships its own tests using inline fixtures. Never depend on another person's module being merged.
- Commit small and often, push your branch. Time-box the first working version to about 90 minutes.
- Conventions: `(lat, lon)` order, degrees; altitude in metres; time = unix seconds UTC; distances km; speeds m/s.
- Passive data only. No transmitting signals, no probing airline/airport systems.
- Anything simulated must say so: synthetic snapshots have `kind="synthetic"`, and every tool output that touches one includes `is_synthetic: true`. Seed hot-zone data is labelled illustrative, never measured.

## File ownership

| Person | Branch | Files |
|---|---|---|
| P1 data + simulator | `p1-data` | `gnsswatch/data.py`, `gnsswatch/simulate.py`, `data/snapshots/*`, `tests/test_data.py`, `tests/test_simulate.py` |
| P2 detector | `p2-detect` | `gnsswatch/detect.py`, `tests/test_detect.py` |
| P3 locator + hot zones | `p3-locate` | `gnsswatch/locate.py`, `gnsswatch/hotzones.py`, `gnsswatch/airports.py`, `data/hotzones_seed.json`, `tests/test_locate.py`, `tests/test_hotzones.py` |
| P4 server + demo | `p4-server` | `gnsswatch/server.py`, `gnsswatch/brief.py`, `gnsswatch/extras.py`, `gnsswatch/stubs.py`, `pyproject.toml`, `README.md`, `demo/*`, `tests/test_server.py` |

## Shared models

Already in `gnsswatch/models.py`: `StateVector`, `InjectedEvent`, `Snapshot`, `Anomaly`, `SourceEstimate`, `RegionCell`, `RouteRisk`. Read it.

## Interfaces (implement EXACTLY these signatures)

```python
# gnsswatch/data.py  (P1)
def fetch_live(bbox: Bbox, polls: int = 8, interval_s: int = 12, name: str = "live") -> Snapshot
def synthetic_baseline(bbox: Bbox, n_aircraft: int = 40, duration_s: int = 1800,
                       step_s: int = 10, seed: int = 0, name: str = "baseline") -> Snapshot
def save_snapshot(s: Snapshot, path: str | None = None) -> str   # default data/snapshots/{name}.json
def load_snapshot(name_or_path: str) -> Snapshot
def list_snapshots() -> list[str]

# gnsswatch/simulate.py  (P1). Never mutate the input; return a new kind="synthetic" Snapshot with `injected` set.
def inject_spoof(s: Snapshot, source: tuple[float, float], radius_km: float,
                 t_start: int, t_end: int, mode: str = "teleport",
                 target: tuple[float, float] | None = None, seed: int = 0,
                 name: str | None = None) -> Snapshot          # mode: teleport | drift | attractor
def inject_jam(s: Snapshot, source: tuple[float, float], radius_km: float,
               t_start: int, t_end: int, seed: int = 0, name: str | None = None) -> Snapshot  # dropout

# gnsswatch/detect.py  (P2)
def detect(s: Snapshot, cfg: "DetectConfig | None" = None) -> list[Anomaly]   # DetectConfig is a dataclass defined in detect.py
def score_flights(anoms: list[Anomaly]) -> dict[str, float]                   # icao24 -> 0..1
def region_summary(anoms: list[Anomaly], cell_deg: float = 1.0) -> list[RegionCell]

# gnsswatch/locate.py  (P3)
def estimate_source(anoms: list[Anomaly]) -> SourceEstimate | None
def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float

# gnsswatch/hotzones.py  (P3). origin/destination = ICAO code (see airports.py) or "lat,lon" string.
def build_hotzones(history: list[Anomaly], cell_deg: float = 1.0) -> dict      # JSON-serialisable
def save_hotzones(hz: dict, path: str = "data/hotzones.json") -> None
def load_hotzones(path: str = "data/hotzones_seed.json") -> dict
def route_points(origin: str, destination: str, n: int = 50) -> list[tuple[float, float]]
def route_risk(origin: str, destination: str, hour_utc: int, hz: dict,
               corridor_km: float = 100.0) -> RouteRisk

# gnsswatch/server.py  (P4): MCP tool names are fixed
# list_snapshots, analyze_snapshot, inject_demo_event, route_check, preflight_brief, get_weather, get_notams
```

## Signals the detector uses (P2), and why

Spoofing can leave a plane's own integrity flags looking normal, so integrity is ONE signal, never the main one. Prefer cross-checks that don't trust GNSS:
- `position_jump`: implied speed between consecutive positions is physically impossible.
- `impossible_speed`: reported speed outside airliner range.
- `baro_geo_mismatch`: barometric vs GNSS altitude disagree far beyond normal.
- `velocity_mismatch`: reported velocity disagrees with velocity implied by position changes.
- `duplicate_id`: same `icao24` at the same time in two far-apart places.
- `integrity_drop`: `nic`/`nacp` low, only when present.
- `signal_dropout`: airborne aircraft stop reporting while neighbours keep reporting (jamming sign).

## Data caveats (P1 and everyone)

- OpenSky's REST feed gives one position per aircraft per poll and no history. Build tracks by polling several times, or use `synthetic_baseline`. NIC/NACp are typically not in that feed, so they are optional and detectors must work without them.
- Public APIs are rate-limited and their auth rules can change: read credentials from env vars, cache everything, and make the demo run fully offline from `data/snapshots/`.

## Merge plan

1. Merge order: `p1-data`, `p2-detect`, `p3-locate`, `p4-server`. Run `python -m pytest` after each merge.
2. Then run `python demo/check_end_to_end.py` (owned by P4). It must: load the clean baseline and find no anomalies, inject a spoof, detect it, estimate the source within a sensible error, and print a route risk. Fix integration issues on a new branch `integration`, not in someone's module branch.
3. Register the server: `claude mcp add gnss-watch -- python -m gnsswatch.server`.
