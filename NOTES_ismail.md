# NOTES: Ismail (P4: server, brief, demo)

## Status
- Done: `server.py` (7 tools, stdio), `brief.py`, `extras.py`, `stubs.py`, `pyproject.toml`, `README.md`, `demo/check_end_to_end.py`, `demo/script.md`, `tests/test_server.py`.
- `python -m pytest`: 3 passed (stubs, offline). `python demo/check_end_to_end.py`: ALL PASS on stubs (stub source error about 74 km).
- Checked over real stdio with an MCP client; the live METAR fetch from aviationweather.gov works.

## Decisions everyone should know
- **mcp is pinned `>=1.10,<2`.** mcp 2.x renamed `mcp.server.fastmcp.FastMCP`, so a plain `pip install mcp` breaks the import.
- The server picks each function from the real module if it exists, else from `stubs.py`. `list_snapshots` returns a `backends` map showing which one is live. A module that exists but fails to import prints a warning to stderr and falls back to stubs, so check stderr after merging.
- `main()` changes directory to the repo root, so relative `data/...` paths work however Claude launches the server.
- `route_check` with a snapshot builds hot zones from that snapshot's anomalies, uses the **snapshot's own UTC hour** rather than `hour_utc`, and sets `basis="observed"`. Without a snapshot it uses `load_hotzones()` (the illustrative seed) at `hour_utc`.
- `analyze_snapshot` on an injected snapshot adds `simulation_check` (source error in km, affected vs flagged) for scoring the detector only.

## Asks for other modules (please check at merge)
- **P1:** `load_snapshot` should raise `FileNotFoundError` for an unknown name, because the server catches it to auto-create `baseline`. Please commit `data/snapshots/baseline.json`. The demo assumes a Baltic bbox `(54, 19, 61, 28)` with route EYVI -> EFHK. If yours differs, tell me and I'll change the route in `demo/script.md` (the e2e check adapts to any bbox).
- **P2:** the e2e check asserts **0 anomalies on the clean baseline**.
- **P3:** `airports.py` needs EYVI and EFHK. `baro_geo_mismatch` anomalies carry the *spoofed* position, so `estimate_source` should weight `position_jump` and `signal_dropout` (true positions) instead. The e2e check requires a source error under 150 km. `route_risk(..., hz)` must accept the dict from `build_hotzones` of a single snapshot.

## Open
- **Not pushed:** there's no git remote yet; the local branch `p4-server` is committed. Add the team remote and run `git push -u origin p4-server`.
- NOTAMs are a labelled SAMPLE unless `FAA_CLIENT_ID`/`FAA_CLIENT_SECRET` are set. The FAA call is untested (no key).
