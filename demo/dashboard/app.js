/*
  GNSS Watch — operations dashboard
  ---------------------------------------
  Standalone vanilla JS. No build step, no framework.

      python -m http.server 8000 --directory demo/dashboard

  Every number this page draws comes from `data/scenarios.json`, which
  `build_data.py` exports by running the real detector over the demo snapshots.
  Nothing here is placeholder data. What this file owns is the projection, the
  replay clock and the drawing — not a single detection threshold.

  Regenerate the JSON after touching detect.py / locate.py:

      PYTHONPATH=. python demo/dashboard/build_data.py

  The interference events are SIMULATED and labelled as such everywhere they
  appear. The violet rings are the injected sources; they exist to score the
  estimates and are never measured values.

  Playback runs continuously and loops; there is no transport bar. Space still
  pauses and the arrow keys still step, for anyone driving it by hand.
*/

(() => {
  'use strict';

  // ── constants ────────────────────────────────────────────────────────
  const TRAIL_FRAMES = 16;    // length of the motion trail
  const BLOOM_FRAMES = 8;     // how long a fresh finding ring stays visible
  const ACTIVE_FRAMES = 6;    // aircraft reads as "currently firing" this long
  const FEED_MAX = 40;        // most recent findings kept in the rail
  // One pass through the window. 2 hours of traffic over 55 s is about 130x
  // real time, putting a fast airliner near 16 px/s on a desktop map. This is
  // the knob to turn if it feels wrong.
  const LOOP_SECONDS = 55;
  const PAD = 36;
  const KM_PER_DEG = 111.32;

  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

  // Airliner planform, nose pointing +x, built once and reused. A plain
  // triangle plus a motion trail reads as a tadpole rather than an aircraft,
  // so this carries swept wings and a tailplane to stay legible at 12 px.
  const PLANE = (() => {
    const p = new Path2D();
    const half = [
      [8.0, 0], [5.2, 1.2], [1.6, 1.3],
      [-1.0, 5.4], [-2.9, 5.4],
      [-2.0, 1.3], [-4.8, 1.3],
      [-5.6, 3.0], [-7.0, 3.0], [-7.2, 0],
    ];
    p.moveTo(half[0][0], half[0][1]);
    for (const [x, y] of half.slice(1)) p.lineTo(x, y);
    for (const [x, y] of half.slice(0, -1).reverse()) p.lineTo(x, -y);
    p.closePath();
    return p;
  })();

  // ── state ────────────────────────────────────────────────────────────
  const S = {
    data: null,
    sc: null,
    t: 0,                 // replay position as a FLOAT frame, for interpolation
    frame: 0,             // Math.floor(S.t); everything discrete keys off this
    playing: false,
    selected: null,
    byFrame: new Map(),   // frame -> episodes starting at that frame
    spanByAc: new Map(),  // icao24 -> [[fi, fi_end], ...]
    lastByAc: new Map(),  // icao24 -> most recent episode frame at or before now
    acIndex: new Map(),
    feedShown: 0,         // episodes currently rendered, to append incrementally
    proj: null,
    dpr: 1,
    acc: 0,
    last: 0,
  };

  const $ = (id) => document.getElementById(id);
  const app = $('app');
  const cv = $('map');
  const ctx = cv.getContext('2d');

  // ── projection ───────────────────────────────────────────────────────
  function project(bbox, w, h) {
    const [lat0, lon0, lat1, lon1] = bbox;
    const kx = Math.cos(((lat0 + lat1) / 2) * Math.PI / 180);
    const spanX = (lon1 - lon0) * kx;
    const spanY = lat1 - lat0;
    const s = Math.min((w - 2 * PAD) / spanX, (h - 2 * PAD) / spanY);
    const ox = (w - spanX * s) / 2;
    const oy = (h - spanY * s) / 2;
    return {
      x: (lon) => ox + (lon - lon0) * kx * s,
      y: (lat) => oy + (lat1 - lat) * s,
      km: (k) => (k / KM_PER_DEG) * s,
      lat0, lon0, lat1, lon1,
    };
  }

  function resize() {
    const r = cv.getBoundingClientRect();
    S.dpr = Math.min(window.devicePixelRatio || 1, 2);
    cv.width = Math.round(r.width * S.dpr);
    cv.height = Math.round(r.height * S.dpr);
    ctx.setTransform(S.dpr, 0, 0, S.dpr, 0, 0);
    if (S.sc) S.proj = project(S.sc.bbox, r.width, r.height);
  }

  // ── map drawing ──────────────────────────────────────────────────────
  function drawGraticule(p, w, h) {
    ctx.save();
    ctx.strokeStyle = 'rgba(126,147,137,0.10)';
    ctx.fillStyle = 'rgba(126,147,137,0.45)';
    ctx.lineWidth = 1;
    ctx.font = '9px "JetBrains Mono", monospace';
    const step = 4;
    for (let lon = Math.ceil(p.lon0 / step) * step; lon <= p.lon1; lon += step) {
      const x = p.x(lon);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
      ctx.fillText(`${lon}°E`, x + 4, h - 8);
    }
    for (let lat = Math.ceil(p.lat0 / step) * step; lat <= p.lat1; lat += step) {
      const y = p.y(lat);
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
      ctx.fillText(`${lat}°N`, 6, y - 5);
    }
    ctx.restore();
  }

  function drawHotCells(p, cells) {
    if (!cells || !cells.length) return;
    const max = Math.max(...cells.map((c) => c.n_anomalies)) || 1;
    ctx.save();
    ctx.lineWidth = 1;
    for (const c of cells) {
      const x0 = p.x(c.lon - 0.5), x1 = p.x(c.lon + 0.5);
      const y0 = p.y(c.lat + 0.5), y1 = p.y(c.lat - 0.5);
      ctx.fillStyle = `rgba(242,169,60,${0.04 + 0.11 * (c.n_anomalies / max)})`;
      ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
      ctx.strokeStyle = 'rgba(242,169,60,0.14)';
      ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
    }
    ctx.restore();
  }

  function drawRoute(p) {
    ctx.save();
    ctx.strokeStyle = 'rgba(191,208,198,0.3)';
    ctx.lineWidth = 1.3;
    ctx.setLineDash([7, 5]);
    ctx.beginPath();
    S.data.route_line.forEach(([la, lo], i) =>
      i ? ctx.lineTo(p.x(lo), p.y(la)) : ctx.moveTo(p.x(lo), p.y(la)));
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.font = '600 10px "JetBrains Mono", monospace';
    for (const a of S.data.airports) {
      const x = p.x(a.lon), y = p.y(a.lat);
      ctx.fillStyle = css('--bg');
      ctx.fillRect(x - 4, y - 4, 8, 8);
      ctx.strokeStyle = css('--text-2');
      ctx.lineWidth = 1.5;
      ctx.strokeRect(x - 4, y - 4, 8, 8);
      ctx.fillStyle = css('--text-2');
      ctx.fillText(a.icao, x + 9, y + 4);
    }
    ctx.restore();
  }

  // Position between two samples. The replay runs far slower than the 10 s
  // sample interval, so without this the traffic would step visibly. A big
  // gap between consecutive samples is a spoofed jump, not motion, so that
  // case snaps instead of sliding across it.
  const JUMP_DEG = 0.6;
  function posAt(ac, t) {
    const f = Math.floor(t), frac = t - f;
    const la0 = ac.lat[f], lo0 = ac.lon[f];
    if (la0 == null) return null;
    const la1 = ac.lat[f + 1], lo1 = ac.lon[f + 1];
    if (la1 == null || frac === 0) return [la0, lo0];
    if (Math.abs(la1 - la0) > JUMP_DEG || Math.abs(lo1 - lo0) > JUMP_DEG) return [la0, lo0];
    return [la0 + (la1 - la0) * frac, lo0 + (lo1 - lo0) * frac];
  }

  function drawAircraft(p, ac, fi) {
    const from = Math.max(0, fi - TRAIL_FRAMES);
    let cur = null, prev = null;
    const now = posAt(ac, S.t);
    if (now) cur = [p.x(now[1]), p.y(now[0])];
    const back = posAt(ac, Math.max(0, S.t - 1));   // a full sample back: stable heading
    if (back) prev = [p.x(back[1]), p.y(back[0])];

    const lastAnom = S.lastByAc.get(ac.icao24);
    const active = lastAnom != null && fi - lastAnom < ACTIVE_FRAMES;
    const flagged = lastAnom != null;
    const isSel = S.selected === ac.icao24;

    // Segment by segment so the trail fades out behind the aircraft. Drawn as
    // one solid stroke it reads as a tail growing off the glyph.
    ctx.save();
    ctx.lineCap = 'round';
    ctx.lineWidth = isSel ? 2 : flagged ? 1.5 : 1;
    ctx.strokeStyle = isSel ? css('--sel') : flagged ? css('--caution') : '#7E9389';
    const span = Math.max(1, fi - from);
    for (let i = from + 1; i <= fi; i++) {
      const la0 = ac.lat[i - 1], lo0 = ac.lon[i - 1];
      const la1 = ac.lat[i], lo1 = ac.lon[i];
      if (la0 == null || la1 == null) continue;   // dropout: break, don't bridge
      const t = (i - from) / span;
      ctx.globalAlpha = t * t * (isSel ? 0.95 : flagged ? 0.8 : 0.42);
      ctx.beginPath();
      ctx.moveTo(p.x(lo0), p.y(la0));
      ctx.lineTo(p.x(lo1), p.y(la1));
      ctx.stroke();
    }
    ctx.restore();

    if (!cur) return;   // mid-dropout: nothing reported at this frame

    const ang = prev ? Math.atan2(cur[1] - prev[1], cur[0] - prev[0]) : 0;

    if (active) {
      const t = (fi - lastAnom) / ACTIVE_FRAMES;
      ctx.beginPath();
      ctx.arc(cur[0], cur[1], 7 + t * 9, 0, Math.PI * 2);
      ctx.strokeStyle = `rgba(242,169,60,${0.5 * (1 - t)})`;
      ctx.lineWidth = 1.4;
      ctx.stroke();
    }

    ctx.save();
    ctx.translate(cur[0], cur[1]);
    ctx.rotate(ang);
    ctx.scale(isSel ? 1.15 : 0.9, isSel ? 1.15 : 0.9);
    ctx.fillStyle = isSel ? css('--sel') : flagged ? css('--caution') : '#93AB9D';
    ctx.fill(PLANE);
    ctx.strokeStyle = css('--bg-map');
    ctx.lineWidth = 0.7;
    ctx.stroke(PLANE);
    ctx.restore();

    if (isSel) {
      ctx.beginPath();
      ctx.arc(cur[0], cur[1], 13, 0, Math.PI * 2);
      ctx.strokeStyle = css('--sel');
      ctx.lineWidth = 1.4;
      ctx.stroke();
      ctx.font = '600 11px "JetBrains Mono", monospace';
      const tw = ctx.measureText(ac.callsign).width;
      ctx.fillStyle = 'rgba(6,10,8,0.9)';
      ctx.fillRect(cur[0] + 17, cur[1] - 9, tw + 12, 18);
      ctx.strokeStyle = css('--sel');
      ctx.lineWidth = 1.2;
      ctx.strokeRect(cur[0] + 17, cur[1] - 9, tw + 12, 18);
      ctx.fillStyle = css('--sel');
      ctx.fillText(ac.callsign, cur[0] + 23, cur[1] + 4);
    }
  }

  // Fresh findings: an expanding ring, plus a "tear" line for position jumps
  // from the last plausible position to where the aircraft claimed to be.
  function drawBlooms(p, fi) {
    for (let f = Math.max(0, fi - BLOOM_FRAMES); f <= fi; f++) {
      const list = S.byFrame.get(f);
      if (!list) continue;
      const t = (fi - f) / BLOOM_FRAMES;
      const fade = 1 - t;
      for (const e of list) {
        const x = p.x(e.lon), y = p.y(e.lat);
        const col = e.kind === 'signal_dropout' ? '47,217,196' : '242,169,60';

        if (e.kind === 'position_jump') {
          const ac = S.acIndex.get(e.icao24);
          const la = ac && ac.lat[f], lo = ac && ac.lon[f];
          if (la != null && lo != null) {
            ctx.save();
            ctx.setLineDash([4, 3]);
            ctx.strokeStyle = `rgba(242,169,60,${0.75 * fade})`;
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            ctx.moveTo(x, y);
            ctx.lineTo(p.x(lo), p.y(la));
            ctx.stroke();
            ctx.restore();
          }
        }

        ctx.beginPath();
        ctx.arc(x, y, 3 + t * 16, 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(${col},${0.6 * fade})`;
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
    }
  }

  // Both SIMULATED zones. They run for the whole window, so they are drawn at a
  // constant size and brightness for the whole replay: no pulse, no fade in or
  // out, nothing that implies one is more current than the other.
  function drawZones(p) {
    for (const z of S.sc.zones) {
      const tx = p.x(z.lon), ty = p.y(z.lat);
      ctx.save();
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = 'rgba(184,166,240,0.55)';
      ctx.lineWidth = 1.3;
      ctx.beginPath();
      ctx.arc(tx, ty, p.km(z.radius_km), 0, Math.PI * 2);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.arc(tx, ty, 4, 0, Math.PI * 2);
      ctx.strokeStyle = css('--truth');
      ctx.stroke();
      ctx.font = '600 9px "JetBrains Mono", monospace';
      ctx.fillStyle = css('--truth');
      ctx.fillText(`SIMULATED ${z.mode.toUpperCase()}`, tx + 9, ty - 8);
      ctx.restore();

      const est = z.estimate;
      if (!est) continue;
      const x = p.x(est.lon), y = p.y(est.lat);

      ctx.save();
      ctx.beginPath();
      ctx.arc(x, y, p.km(est.radius_km), 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(47,217,196,0.10)';
      ctx.fill();
      ctx.setLineDash([6, 4]);
      ctx.strokeStyle = css('--computed');
      ctx.lineWidth = 1.6;
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.lineWidth = 1.8;
      ctx.beginPath();
      ctx.moveTo(x - 11, y); ctx.lineTo(x + 11, y);
      ctx.moveTo(x, y - 11); ctx.lineTo(x, y + 11);
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(x, y, 3.2, 0, Math.PI * 2);
      ctx.fillStyle = css('--computed');
      ctx.fill();
      ctx.restore();
    }
  }

  function render() {
    if (!S.sc || !S.proj) return;
    const r = cv.getBoundingClientRect();
    const p = S.proj, fi = S.frame;

    ctx.clearRect(0, 0, r.width, r.height);
    drawGraticule(p, r.width, r.height);
    if (S.sc.episodes.length && fi >= S.sc.episodes[0].fi) drawHotCells(p, S.sc.hot_cells);
    drawRoute(p);

    for (const ac of S.sc.aircraft) if (!S.lastByAc.has(ac.icao24)) drawAircraft(p, ac, fi);
    for (const ac of S.sc.aircraft) if (S.lastByAc.has(ac.icao24)) drawAircraft(p, ac, fi);

    drawBlooms(p, fi);
    drawZones(p);
  }

  // ── panels ───────────────────────────────────────────────────────────
  function fmtClock(fi) {
    const d = new Date((S.sc.t_start + fi * S.sc.step_s) * 1000);
    const pad = (x) => String(x).padStart(2, '0');
    return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}Z`;
  }

  const kindLabel = (k) => (S.data.kind_label && S.data.kind_label[k]) || k;

  function updateReadouts() {
    const fi = S.frame;
    let eps = 0;
    const flagged = new Set();
    for (const e of S.sc.episodes) {
      if (e.fi > fi) break;
      eps++;
      flagged.add(e.icao24);
    }
    $('ro-tracked').textContent = S.sc.counts.n_aircraft;
    $('ro-flagged').textContent = flagged.size;
    $('ro-eps').textContent = eps;
    $('clock').textContent = fmtClock(fi);

    // Both zones are live throughout, so the card cycles between them on a slow
    // timer rather than tracking the replay position.
    const zones = S.sc.zones.filter((z) => z.estimate);
    $('zone-card').hidden = !zones.length;
    $('clear-card').hidden = S.sc.episodes.length > 0;

    if (zones.length) {
      const z = zones[Math.floor(Date.now() / 6000) % zones.length];
      const e = z.estimate;
      $('zone-title').textContent = `Located ${z.mode}`;
      $('zone-live').textContent = `${zones.indexOf(z) + 1}/${zones.length}`;
      $('zone-coords').textContent =
        `${Math.abs(e.lat).toFixed(2)}°${e.lat >= 0 ? 'N' : 'S'}  ` +
        `${Math.abs(e.lon).toFixed(2)}°${e.lon >= 0 ? 'E' : 'W'}`;
      $('zone-meta').textContent =
        `±${Math.round(e.radius_km)} km · conf ${e.confidence.toFixed(2)} · ${e.method}`;
      $('zone-err').textContent =
        `${e.error_km} km from SIMULATED truth · ${z.n_affected} aircraft affected`;
    }
    if (!S.sc.episodes.length) {
      $('clear-sub').textContent = `0 findings across ${S.sc.counts.n_aircraft} aircraft`;
    }
  }

  // Findings appear as the replay reaches them. Rebuilding the whole list every
  // frame would restart the entry animation, so entries are appended and only a
  // rewind rebuilds from scratch.
  function renderFeed(rebuild) {
    const box = $('feed');
    const due = S.sc.episodes.filter((e) => e.fi <= S.frame);

    if (rebuild || due.length < S.feedShown) {
      box.innerHTML = '';
      S.feedShown = 0;
    }
    for (let i = S.feedShown; i < due.length; i++) box.prepend(feedRow(due[i]));
    while (box.children.length > FEED_MAX) box.lastElementChild.remove();
    S.feedShown = due.length;

    $('feed-count').textContent = due.length;
    $('feed-empty').hidden = due.length > 0;
  }

  function feedRow(e) {
    const ac = S.acIndex.get(e.icao24);
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'feed-row' + (e.kind === 'signal_dropout' ? ' feed-row--drop' : '');
    row.dataset.icao = e.icao24;
    row.setAttribute('role', 'listitem');
    row.setAttribute('aria-selected', String(S.selected === e.icao24));

    const time = document.createElement('span');
    time.className = 'fd-time';
    time.textContent = fmtClock(e.fi).slice(0, 5);

    const cs = document.createElement('span');
    cs.className = 'fd-cs';
    cs.textContent = ac ? ac.callsign : e.icao24;

    const kind = document.createElement('span');
    kind.className = 'fd-kind';
    kind.textContent = kindLabel(e.kind);

    const detail = document.createElement('span');
    detail.className = 'fd-detail';
    detail.textContent = e.detail + (e.n_obs > 1 ? `  (${e.n_obs} obs)` : '');

    row.append(time, cs, kind, detail);
    row.addEventListener('click', () => select(S.selected === e.icao24 ? null : e.icao24));
    return row;
  }

  // Every aircraft on its cruise level. Built once per scenario; only the tick
  // colours change as the replay flags things, so there is no per-frame churn.
  function buildLadder() {
    const box = $('ladder');
    box.innerHTML = '';
    const byFl = new Map();
    for (const ac of S.sc.aircraft) {
      if (!byFl.has(ac.fl)) byFl.set(ac.fl, []);
      byFl.get(ac.fl).push(ac);
    }
    for (const fl of [...byFl.keys()].sort((a, b) => b - a)) {
      const list = byFl.get(fl).sort((a, b) => a.callsign.localeCompare(b.callsign));
      const row = document.createElement('div');
      row.className = 'lad-row';

      const label = document.createElement('span');
      label.className = 'lad-fl';
      label.textContent = `FL${String(fl).padStart(3, '0')}`;

      const bar = document.createElement('div');
      bar.className = 'lad-bar';
      for (const ac of list) {
        const tick = document.createElement('button');
        tick.type = 'button';
        tick.className = 'lad-tick';
        tick.dataset.icao = ac.icao24;
        tick.title = `${ac.callsign} · FL${String(fl).padStart(3, '0')}`;
        tick.setAttribute('aria-label', tick.title);
        tick.addEventListener('click', () => select(S.selected === ac.icao24 ? null : ac.icao24));
        bar.appendChild(tick);
      }

      const n = document.createElement('span');
      n.className = 'lad-n';
      n.textContent = list.length;

      row.append(label, bar, n);
      box.appendChild(row);
    }

    $('ladder-count').textContent = S.sc.counts.n_aircraft;
    const sep = S.data.separation;
    $('ladder-note').textContent =
      `${[...byFl.keys()].length} levels · ${sep.close_pairs} pairs closed inside ` +
      `${sep.sep_nm} NM, all vertically separated · 0 losses`;
  }

  function paintLadder() {
    for (const tick of document.querySelectorAll('.lad-tick')) {
      const icao = tick.dataset.icao;
      tick.dataset.flag = S.lastByAc.has(icao) ? '1' : '0';
      tick.dataset.sel = S.selected === icao ? '1' : '0';
    }
  }

  function select(icao) {
    S.selected = icao;
    for (const row of document.querySelectorAll('.feed-row')) {
      row.setAttribute('aria-selected', String(row.dataset.icao === icao));
    }
    paintLadder();
  }

  // ── scenario / frame ─────────────────────────────────────────────────
  function setScenario(key) {
    // Both snapshots share a time grid, so holding the frame across a switch
    // makes the toggle a direct A/B of the same instant.
    const keep = S.sc ? S.frame : 0;
    S.sc = S.data.scenarios[key];
    S.selected = null;

    S.byFrame = new Map();
    S.spanByAc = new Map();
    for (const e of S.sc.episodes) {
      if (!S.byFrame.has(e.fi)) S.byFrame.set(e.fi, []);
      S.byFrame.get(e.fi).push(e);
      if (!S.spanByAc.has(e.icao24)) S.spanByAc.set(e.icao24, []);
      S.spanByAc.get(e.icao24).push([e.fi, e.fi_end]);
    }
    S.acIndex = new Map(S.sc.aircraft.map((a) => [a.icao24, a]));

    app.dataset.scenario = key;
    $('btn-clean').setAttribute('aria-pressed', String(key === 'clean'));
    $('btn-spoof').setAttribute('aria-pressed', String(key === 'spoof'));

    const r = cv.getBoundingClientRect();
    S.proj = project(S.sc.bbox, r.width, r.height);

    buildLadder();
    setFrame(keep, true);
  }

  function setFrame(t, rebuild) {
    S.t = Math.max(0, Math.min(S.sc.n_frames - 1, t));
    S.frame = Math.floor(S.t);

    // Most recent episode at or before this frame, per aircraft. An aircraft
    // inside an episode's span counts as firing right now.
    S.lastByAc = new Map();
    for (const [icao, spans] of S.spanByAc) {
      let last = null;
      for (const [a, b] of spans) {
        if (a > S.frame) break;
        last = S.frame <= b ? S.frame : Math.max(last == null ? a : last, b);
      }
      if (last != null) S.lastByAc.set(icao, last);
    }

    updateReadouts();
    renderFeed(rebuild);
    paintLadder();
  }

  // ── replay loop ──────────────────────────────────────────────────────
  function setPlaying(on) {
    S.playing = on;
    app.dataset.playing = on ? '1' : '0';
    S.last = 0;
    S.acc = 0;
  }

  function tick(now) {
    requestAnimationFrame(tick);
    if (!S.sc) return;
    if (S.playing) {
      const dt = now - (S.last || now);
      S.last = now;
      // One pass over the window takes LOOP_SECONDS regardless of how many
      // samples it holds; positions are interpolated between them.
      const perMs = (S.sc.n_frames - 1) / (LOOP_SECONDS * 1000);
      const next = S.t + dt * perMs;
      const wrapped = next >= S.sc.n_frames - 1;
      setFrame(wrapped ? 0 : next, wrapped);    // loop, rebuilding the feed
    } else {
      S.last = now;
    }
    render();
  }

  // ── interaction ──────────────────────────────────────────────────────
  cv.addEventListener('click', (e) => {
    if (!S.sc || !S.proj) return;
    const r = cv.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    let best = null, bestD = 18 * 18;
    for (const ac of S.sc.aircraft) {
      const la = ac.lat[S.frame], lo = ac.lon[S.frame];
      if (la == null) continue;
      const dx = S.proj.x(lo) - mx, dy = S.proj.y(la) - my;
      const d = dx * dx + dy * dy;
      if (d < bestD) { bestD = d; best = ac.icao24; }
    }
    select(best);
  });

  $('btn-clean').addEventListener('click', () => setScenario('clean'));
  $('btn-spoof').addEventListener('click', () => setScenario('spoof'));

  window.addEventListener('keydown', (e) => {
    const t = e.target.tagName;
    if (t === 'INPUT' || t === 'BUTTON' || t === 'TEXTAREA') return;
    if (e.code === 'Space') { e.preventDefault(); setPlaying(!S.playing); }
    else if (e.code === 'ArrowRight') { setPlaying(false); setFrame(S.frame + (e.shiftKey ? 10 : 1)); }
    else if (e.code === 'ArrowLeft') { setPlaying(false); setFrame(S.frame - (e.shiftKey ? 10 : 1), true); }
  });

  window.addEventListener('resize', resize);

  // ── boot ─────────────────────────────────────────────────────────────
  fetch('data/scenarios.json')
    .then((r) => {
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      return r.json();
    })
    .then((d) => {
      S.data = d;
      $('gen').textContent = `exported ${d.generated_utc}`;
      app.dataset.state = 'ready';
      resize();
      setScenario('spoof');
      requestAnimationFrame(tick);
      setPlaying(true);
    })
    .catch((err) => {
      app.dataset.state = 'error';
      $('loading').innerHTML =
        `<div style="text-align:center;max-width:440px;line-height:1.65">
           <strong style="color:var(--caution-text)">No detector output found.</strong><br>
           <span style="font-size:11.5px">${String(err)}</span><br><br>
           <span style="font-size:11.5px">Generate it from the repo root:</span><br>
           <code style="font-size:11px;color:var(--computed)">PYTHONPATH=. python demo/dashboard/build_data.py</code><br><br>
           <span style="font-size:11px;color:var(--text-muted)">then serve this folder over http:// — fetch() does not work from file://</span>
         </div>`;
    });
})();
