"""Smoke test: list the MCP tools and call each once, against stubs and offline."""
import asyncio
import json
import os

os.environ["GNSSWATCH_STUBS"] = "1"
os.environ["GNSSWATCH_OFFLINE"] = "1"

from gnsswatch import server  # noqa: E402

EXPECTED = {"list_snapshots", "analyze_snapshot", "inject_demo_event", "route_check",
            "preflight_brief", "get_weather", "get_notams"}


def call(tool: str, **args) -> dict:
    result = asyncio.run(server.mcp.call_tool(tool, args))
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


def test_tools_listed():
    assert {t.name for t in asyncio.run(server.mcp.list_tools())} == EXPECTED
    assert set(server.BACKENDS.values()) == {"stub"}


def test_each_tool_once():
    snaps = call("list_snapshots")
    assert "baseline" in [s["name"] for s in snaps["snapshots"]]

    clean = call("analyze_snapshot", name="baseline")
    assert clean["counts"]["n_anomalies"] == 0

    ev = call("inject_demo_event", base="baseline", mode="teleport", source_lat=57.8, source_lon=24.0,
              radius_km=150, name="t_spoof")
    assert ev["is_synthetic"] is True and ev["n_affected_aircraft"] > 0

    spoofed = call("analyze_snapshot", name="t_spoof")
    assert spoofed["is_synthetic"] is True
    assert spoofed["counts"]["n_anomalies"] > 0
    assert spoofed["simulation_check"]["affected_and_flagged"] > 0

    seed = call("route_check", origin="EYVI", destination="EFHK", hour_utc=12)
    assert seed["basis"] == "illustrative_seed" and 0 <= seed["score"] <= 1

    rc = call("route_check", origin="EYVI", destination="EFHK", snapshot="t_spoof")
    assert rc["is_synthetic"] is True and rc["score"] > 0

    pb = call("preflight_brief", origin="EYVI", destination="EFHK", snapshot="t_spoof")
    assert pb["is_synthetic"] is True and "SIMULATED" in pb["fallback_text"]

    wx = call("get_weather", icao="EVRA")
    assert wx["live"] is False and wx["raw"]

    nt = call("get_notams", icao="EVRA")
    assert nt["is_sample"] is True


def test_bad_inputs_do_not_crash():
    assert "error" in call("inject_demo_event", mode="nonsense")
    assert "error" in call("get_weather", icao="not-an-icao")
