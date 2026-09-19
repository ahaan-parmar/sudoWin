# 90-second demo

Setup (before going on stage): `source .venv/bin/activate`, `python demo/check_end_to_end.py` must print ALL PASS. `claude mcp list` shows `gnss-watch`. Set `GNSSWATCH_OFFLINE=1` if the venue Wi-Fi is bad (weather falls back to labelled samples). Start `claude` in the repo root with a large terminal font.

Route used: **EYVI (Vilnius) -> EFHK (Helsinki)**, which crosses the Baltic baseline area (bbox 54-61N, 19-28E). If the merged baseline covers another area, pick two airports on either side of it, and leave out source_lat/source_lon so the event lands at the centre of the area.

| Time | Say / type | What should happen |
|---|---|---|
| 0-10 s | "Spoofing reports rose 193% (IATA 2025). We can't stop a fake signal; we stop it from mattering." | none |
| 10-25 s | Ask: **"Is the route from EYVI to EFHK affected right now? Use the baseline snapshot."** | `route_check(snapshot="baseline")` returns risk **LOW, 0 flagged cells**. The assistant may call `analyze_snapshot` too: 0 anomalies. |
| 25-40 s | Ask: **"Simulate a spoofing event centred at 57.8, 24.0 with a 150 km radius."** | `inject_demo_event` creates `demo_teleport`, labelled **SIMULATED**, with N aircraft affected. |
| 40-65 s | Ask: **"Now check EYVI to EFHK again against demo_teleport. Where is it coming from?"** | Risk goes to **HIGH**. Flagged aircraft show position jumps and baro/GNSS altitude mismatch. Source estimate with radius and confidence; `simulation_check.source_error_km` gives the error against ground truth. |
| 65-85 s | Ask: **"Write the pre-flight brief for the crew."** | `preflight_brief` returns the evidence pack, METARs and NOTAMs (NOTAMs marked SAMPLE), and a brief with actions: cross-check with DME/VOR/IRS, expect false terrain alerts, check NOTAMs. Caveats kept. |
| 85-90 s | "Detection is deterministic code. The AI only picks tools and explains, so it can't invent an alert." | none |

Things to point out: every simulated output says `is_synthetic: true`; the clean baseline raised zero false alarms.

## Fallback video

1. Record the whole demo once when it runs cleanly. On Fedora/GNOME press Ctrl+Shift+Alt+R to start and stop (saves to ~/Videos/Screencasts), or use OBS. Record at 1080p with the terminal full screen.
2. Also record `python demo/check_end_to_end.py`. It takes 10 seconds, needs no network or LLM, and shows detection, locate and route risk.
3. Keep both on the laptop and a USB stick, and don't rely on the venue network. If the live demo stalls for more than 10 s, switch to the video.
