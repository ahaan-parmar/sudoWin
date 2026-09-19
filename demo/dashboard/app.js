/*
  GNSS Watch — operations dashboard demo
  ---------------------------------------
  Standalone vanilla JS. No build step, no framework, no dependency on the
  Claude Design runtime the prototype was authored in — this is a plain
  static page you can open directly or serve with any static file server:

      python -m http.server 8000 --directory demo/dashboard

  Scope: this reproduces the interaction design and sample data from the
  design prototype (GNSS Watch.dc.html), restyled. It is NOT wired to the
  live gnsswatch backend — every number here is placeholder sample data,
  same as the prototype's own disclaimer said. Wiring it up would mean
  serving gnsswatch.brief.preflight_brief() / analyze_snapshot() output
  (Anomaly, SourceEstimate, RouteRisk from gnsswatch/models.py) as JSON to
  replace the FLIGHTS/BRIEF constants below — left as a follow-up, not
  attempted here.
*/

(() => {
  'use strict';

  const FLIGHTS = [
    {
      callsign: 'FIN1342', icao: '461f8c', score: '0.91', top: 'Position jump',
      x: 302.1, y: 183.5,
      chip: { x: 194, y: 181.5, tx: 9, ty: 15 },
      anoms: [
        { glyph: '◆', kind: 'Position jump', sev: 'High', detail: '38 km lateral step in 4 s' },
        { glyph: '▲', kind: 'Baro/GNSS alt mismatch', sev: 'Medium', detail: 'baro 34,000 ft vs GNSS 31,200 ft' },
        { glyph: '✕', kind: 'Signal dropout', sev: 'Low', detail: '11 s gap, NIC dropped to 0' },
      ],
    },
    {
      callsign: 'SAS2718', icao: '4ac9d1', score: '0.84', top: 'Baro/GNSS alt mismatch',
      x: 372.4, y: 220.7,
      chip: { x: 264.4, y: 218.7, tx: 9, ty: 15 },
      anoms: [
        { glyph: '▲', kind: 'Baro/GNSS alt mismatch', sev: 'High', detail: 'baro 37,000 ft vs GNSS 33,400 ft' },
        { glyph: '■', kind: 'Velocity mismatch', sev: 'Medium', detail: 'GS 462 kt vs derived 511 kt' },
      ],
    },
    {
      callsign: 'AY1553', icao: '461cb2', score: '0.77', top: 'Velocity mismatch',
      x: 328.7, y: 252.3,
      chip: { x: 220.7, y: 250.3, tx: 9, ty: 15 },
      anoms: [
        { glyph: '■', kind: 'Velocity mismatch', sev: 'High', detail: 'GS 448 kt vs derived 596 kt' },
        { glyph: '◆', kind: 'Position jump', sev: 'Medium', detail: '17 km lateral step in 6 s' },
      ],
    },
    {
      callsign: 'BTI4H', icao: '502c9a', score: '0.69', top: 'Signal dropout',
      x: 296.4, y: 223.6,
      chip: { x: 188.4, y: 221.6, tx: 9, ty: 15 },
      anoms: [
        { glyph: '✕', kind: 'Signal dropout', sev: 'Medium', detail: '48 s gap over cell 19.5E/56.5N' },
        { glyph: '◆', kind: 'Position jump', sev: 'Low', detail: '9 km lateral step in 5 s' },
      ],
    },
    {
      callsign: 'LOT358', icao: '489a22', score: '0.62', top: 'Duplicate ID',
      x: 294.5, y: 186.3,
      chip: { x: 186.5, y: 184.3, tx: 9, ty: 15 },
      anoms: [
        { glyph: '●', kind: 'Duplicate ID', sev: 'Medium', detail: 'ICAO24 489a22 seen at two positions' },
        { glyph: '✕', kind: 'Signal dropout', sev: 'Low', detail: '14 s gap, NIC dropped to 1' },
      ],
    },
  ];

  const BRIEF_SIM = 'Simulated: 5 of 38 aircraft over the eastern Baltic show ADS-B signs of GNSS interference, clustered near 56.94°N 21.08°E — on the EFHK–EDDF corridor.';
  const BRIEF_CLEAN = 'None of the 38 tracked aircraft show ADS-B signs of GNSS interference in this window, and no source estimate was produced.';

  const SEV_CLASS = { High: 'sev-chip--high', Medium: 'sev-chip--medium', Low: 'sev-chip--low' };

  const state = {
    scenario: 'sim',      // 'sim' | 'clean'
    selected: 'FIN1342',  // callsign or null
    showTrue: true,
    timeIdx: 25,
    copied: false,
  };

  const $ = (id) => document.getElementById(id);
  const app = $('app');

  function timeLabel(idx) {
    const mins = 40 + idx;
    return mins >= 60 ? `14:${String(mins - 60).padStart(2, '0')}` : `13:${String(mins).padStart(2, '0')}`;
  }

  function renderFlightRow(f) {
    const selected = f.callsign === state.selected;
    const row = document.createElement('div');

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'flight-row';
    btn.dataset.callsign = f.callsign;
    btn.setAttribute('aria-expanded', String(selected));
    btn.innerHTML = `
      <span class="fr-callsign">${f.callsign}</span>
      <span class="fr-icao">${f.icao}</span>
      <span class="fr-score">${f.score}</span>
      <span class="fr-top"><span class="fr-top-text">${f.top}</span><span class="fr-caret" aria-hidden="true">▸</span></span>
    `;
    btn.addEventListener('click', () => {
      state.selected = state.selected === f.callsign ? null : f.callsign;
      state.copied = false;
      render();
      const el = $('flights-list').querySelector(`[data-callsign="${CSS.escape(f.callsign)}"]`);
      if (el) el.scrollIntoView({ block: 'nearest' });
    });
    row.appendChild(btn);

    if (selected) {
      const detail = document.createElement('div');
      detail.className = 'flight-detail';
      const anomsHtml = f.anoms.map((a) => `
        <div class="anomaly-line">
          <span class="al-kind"><span class="al-glyph">${a.glyph}</span>${a.kind}</span>
          <span class="sev-chip ${SEV_CLASS[a.sev]}">${a.sev}</span>
          <span class="al-detail">${a.detail}</span>
        </div>
      `).join('');
      detail.innerHTML = `<div class="flight-detail-title">Anomalies · ${f.callsign}</div>${anomsHtml}`;
      row.appendChild(detail);
    }
    return row;
  }

  function render() {
    const sim = state.scenario === 'sim';

    app.dataset.scenario = sim ? 'sim' : 'clean';
    app.dataset.showTrue = String(state.showTrue);

    // Header
    $('btn-clean').setAttribute('aria-pressed', String(!sim));
    $('btn-sim').setAttribute('aria-pressed', String(sim));
    $('flagged-num').textContent = sim ? '5' : '0';
    $('flagged-count-2').textContent = sim ? '5' : '0';
    $('flagged-card').classList.toggle('is-clear', !sim);

    // Anomaly-by-kind counts
    $('c-jump').textContent = sim ? '7' : '0';
    $('c-alt').textContent = sim ? '5' : '0';
    $('c-vel').textContent = sim ? '4' : '0';
    $('c-dup').textContent = sim ? '2' : '0';
    $('c-drop').textContent = sim ? '3' : '0';

    // True-source toggle button
    $('true-btn').setAttribute('aria-pressed', String(state.showTrue));
    $('true-btn-box').textContent = state.showTrue ? '☑' : '☐';

    // Per-flight track / marker emphasis: dim everyone but the selection,
    // widen the selected flight's track.
    const sel = sim ? state.selected : null;
    document.querySelectorAll('#flagged-tracks polyline').forEach((el) => {
      const owner = el.dataset.owner;
      const active = !sel || FLIGHTS[Number(owner) - 1].callsign === sel;
      el.style.stroke = active ? 'var(--caution)' : 'rgba(242, 169, 60, 0.3)';
      el.style.strokeWidth = FLIGHTS[Number(owner) - 1].callsign === sel ? '5' : '3.5';
    });
    document.querySelectorAll('#flight-markers path').forEach((el) => {
      const owner = el.dataset.owner;
      const active = !sel || FLIGHTS[Number(owner) - 1].callsign === sel;
      el.setAttribute('fill', active ? 'var(--caution)' : 'rgba(242, 169, 60, 0.3)');
    });

    // Selection ring + chip
    const selRing = $('sel-ring');
    const selF = sel ? FLIGHTS.find((f) => f.callsign === sel) : null;
    if (selF) {
      selRing.hidden = false;
      $('sel-ring-outer').setAttribute('cx', selF.x);
      $('sel-ring-outer').setAttribute('cy', selF.y + 4);
      $('sel-ring-halo').setAttribute('cx', selF.x);
      $('sel-ring-halo').setAttribute('cy', selF.y + 4);
      $('sel-chip-rect').setAttribute('x', selF.chip.x);
      $('sel-chip-rect').setAttribute('y', selF.chip.y);
      const text = $('sel-chip-text');
      text.setAttribute('x', selF.chip.x);
      text.setAttribute('y', selF.chip.y);
      text.textContent = selF.callsign;
    } else {
      selRing.hidden = true;
    }

    // Flagged flights table
    $('clean-note').hidden = sim;
    $('flights-table').style.display = sim ? 'flex' : 'none';
    const list = $('flights-list');
    list.innerHTML = '';
    if (sim) {
      FLIGHTS.forEach((f) => list.appendChild(renderFlightRow(f)));
    }

    // Route risk
    $('risk-score').textContent = sim ? '0.68' : '—';
    $('risk-score').classList.toggle('is-clear', !sim);
    $('risk-label').textContent = sim ? 'elevated' : 'no signs detected';
    $('risk-bar').style.width = sim ? '68%' : '0%';

    // Time
    $('time-label').textContent = `${timeLabel(state.timeIdx)} UTC`;
    $('time-range').value = String(state.timeIdx);

    // Brief + copy button
    $('brief-text').textContent = sim ? BRIEF_SIM : BRIEF_CLEAN;
    $('copy-btn').textContent = state.copied ? 'Copied' : 'Copy';
    $('copy-btn').classList.toggle('is-copied', state.copied);
  }

  // ---- wiring ------------------------------------------------------------

  $('btn-clean').addEventListener('click', () => {
    state.scenario = 'clean';
    state.copied = false;
    render();
  });
  $('btn-sim').addEventListener('click', () => {
    state.scenario = 'sim';
    state.copied = false;
    render();
  });

  $('true-btn').addEventListener('click', () => {
    state.showTrue = !state.showTrue;
    render();
  });

  $('time-range').addEventListener('input', (e) => {
    state.timeIdx = Number(e.target.value);
    render();
  });

  $('copy-btn').addEventListener('click', async () => {
    const text = state.scenario === 'sim' ? BRIEF_SIM : BRIEF_CLEAN;
    try {
      if (navigator.clipboard) await navigator.clipboard.writeText(text);
    } catch (_) {
      /* clipboard permissions denied — button still reflects the attempt */
    }
    state.copied = true;
    render();
    setTimeout(() => {
      state.copied = false;
      render();
    }, 1600);
  });

  render();
})();
