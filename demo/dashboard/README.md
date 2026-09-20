# Operations dashboard

A static page that replays a snapshot and shows what the detector found in it.
Four files, no build step, no framework, no new dependencies.

## Run it

From the repo root:

```bash
PYTHONPATH=. python demo/dashboard/build_data.py
python -m http.server 8000 --directory demo/dashboard
```

Then open http://localhost:8000.

It has to be served over http. The page fetches its data, and `fetch()` is
blocked on `file://`, so opening `index.html` directly shows an error panel
saying the same thing.

`data/scenarios.json` is committed, so the second command alone is enough if you
have not changed any detector code. The working snapshots `build_data.py` writes
into `data/snapshots/dash_*.json` are about 40 MB and are gitignored; they are
rebuilt deterministically from a fixed seed.

## Where the numbers come from

`build_data.py` builds the two demo snapshots, runs the same functions the MCP
tools expose over them, and writes `data/scenarios.json`:

| Scenario | Traffic | What the detector returns |
|---|---|---|
| Clean | 120 aircraft, 2 h | 0 findings, no located source |
| Interference | same traffic, 2 SIMULATED zones | 307 findings on 98 aircraft, both zones located |

Both zones run for the whole window and sit on the map at a constant size:
a teleport spoof at 19.5N 79.5E (located to 148 km) and a drift spoof at
13.5N 74.0E (located to 6 km). 98 of 98 affected aircraft flagged, no false
positives.

Every figure on screen is that output: findings with the detector's own wording,
per-aircraft risk scores, hot cells, and each zone's located source with its
radius, confidence and method.

The page itself owns the map projection, the replay clock and the drawing. It
contains no thresholds and no sample figures.

Re-run `build_data.py` after changing `detect.py` or `locate.py`, or the page
keeps showing the previous run's numbers.

## What build_data.py adds on top of the pipeline

**Flight levels.** `synthetic_baseline` picks each aircraft's cruise level from
about eleven values, so with 120 aircraft some pairs end up co-altitude and
converging. That is a collision, not a demo. The script builds the conflict
graph (any pair that ever closes inside 5 NM laterally) and gives each aircraft
a level none of its conflict partners holds, drawn at random from FL280–FL410 so
the traffic looks like traffic. Barometric and GNSS altitude move together by
the same delta, so the offset the generator chose is preserved and the detector
stays quiet. It then verifies the result: 100 close pairs, 0 separation losses,
all vertically separated by at least 1000 ft. The clean snapshot must still
return zero findings, and `main()` asserts that.

**Speeds.** `synthetic_baseline` draws cruise speed from 200-250 m/s, a 25%
spread that reads on screen as everything moving at the same rate. Each
aircraft's track is time-warped by a per-aircraft factor so the spread is closer
to 2x: 134-242 m/s, or 261-470 kt. Only slowing down, never speeding up, since a
factor above 1 would run off the end of the generated leg. Reported velocity and
track are recomputed from the warped positions exactly as the generator does --
break that invariant and `velocity_mismatch` fires across the clean baseline.

**Episodes.** The detector emits one anomaly per suspicious *observation*, which
is correct: one aircraft sitting in a spoofed cell for ten minutes is genuinely
bad on every sample. But 21,347 observations is not 21,347 events. An episode is
one aircraft, one kind, one continuous run, which brings that to 307 findings
(192 position jumps, 111 altitude episodes, 4 velocity mismatches). The raw count
is kept alongside it, never discarded.

The **altitude ladder** in the rail is that separation work made visible: every
aircraft on its cruise level, amber where the detector has flagged it.

## Two sources, one single-source locator

`estimate_source` solves for a single emitter. With both zones live at once it
converged on a confident-looking point between them, about 610 km from either
truth. `cluster_anomalies` splits the anomalies into spatially separate groups
first, using only their positions and never the injected truth, and the real
locator then runs once per cluster. The clusters are matched to zones afterwards
purely to score them.

A proper version of this belongs in `locate.py`; `NOTES_ahaan.md` already flags
the single-source assumption.

Two things that did not work, so you do not repeat them:

* **A jam zone running the whole window.** Jammed aircraft simply never report,
  so there is almost nothing to localise and the estimate degraded to ~280 km.
  Jamming needs a time window; both permanent zones are spoofing.
* **Minimal graph colouring for flight levels.** It satisfies separation with
  two levels, which is neither realistic nor worth looking at. Levels are now
  drawn at random from the whole band, restricted to what an aircraft's conflict
  partners do not already hold.

## Reading the screen

Colours follow the glass-cockpit convention the stylesheet documents: amber is
flagged or anomalous, teal-cyan is computed or estimated, green is clear, white
is whatever you have selected, and violet is SIMULATED ground truth.

The violet rings are the injected events at their real injection points and
radii. They exist to score the estimates and are never measured values. The teal
rings are what the locator worked out from the anomalies alone, without being
told the answer.

Counters are cumulative to the current frame, so they climb as the detector
finds things rather than showing the final total from the start. Dashed amber
lines are position jumps, drawn from the last plausible position to where the
aircraft claimed to be. A trail that breaks is a dropout, not a rendering bug.

## Controls

Playback loops continuously. One pass over the 2-hour window takes about 55
seconds (`LOOP_SECONDS` in app.js, the knob to turn if the pace feels wrong),
which is roughly 130x real time and puts a fast airliner near 16 px/s on a
desktop map. Positions are interpolated between samples so the traffic moves
smoothly rather than stepping. A position jump is not interpolated: it snaps,
because that is what it is.

How far an aircraft gets across the map is set by how long the window is, not by
how fast the loop runs. At 80 minutes each one covered barely half the box and
the traffic looked becalmed however fast it played.

There is no transport bar and no progress indicator. Space pauses, the arrow
keys step a frame (hold shift for ten). Click an aircraft on the map, any row in
the findings feed, or any tick in the altitude ladder to select it. Switching
scenario holds the current frame, so the toggle is a direct A/B of the same
instant.
