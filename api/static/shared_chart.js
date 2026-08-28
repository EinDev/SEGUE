// SEGUE — shared sparkline chart helper (admin + DJ views).
//
// No vendored charting library: this project is deliberately vanilla JS
// with no build step (see CONCEPT.md §3.3 - fewer moving parts, less to
// break on event night). A 5-minute rolling window at a 5s server-side
// sample interval is 60 points, well within hand-rolled-SVG territory.
//
// The 5-minute history itself is computed and stored server-side (see
// api/app/main.py's _history_collector_loop) so every viewer - the DJ's
// own dashboard and the admin's details panel - sees the same backfilled
// window immediately on open, rather than each starting from an empty
// chart. This module is purely a renderer: it takes whatever "samples"
// array the api handed back in a JSON response and draws it. No state
// is kept here between calls.

window.SegueChart = (function () {
  "use strict";

  const WINDOW_MS = 5 * 60 * 1000;

  // Turns the api's raw sample list (as returned in a stats response's
  // "history" field: [{ts: "2026-...Z", bitrate_kbps, delay_seconds}, ...])
  // into the {t: epochMs, v: number|null} series `renderSparkline` wants,
  // picking out one field.
  function toSeries(historySamples, field) {
    return (historySamples || []).map((s) => ({
      t: new Date(s.ts).getTime(),
      v: s[field],
    }));
  }

  // Renders into `wrapEl` (an empty-state message or an <svg> sparkline)
  // and, if given, a short "current / min-max" caption into `captionEl`.
  // `series`: array of {t: epochMs, v: number|null}, oldest first - v of
  // null marks a gap (e.g. the DJ was disconnected at that sample) and
  // breaks the line there instead of interpolating across it.
  // opts: { unit: string, decimals: number, colorClass: string }
  function renderSparkline(wrapEl, captionEl, series, opts) {
    opts = opts || {};
    const unit = opts.unit || "";
    const decimals = opts.decimals != null ? opts.decimals : 1;
    const colorClass = opts.colorClass || "";

    const now = Date.now();
    const windowStart = now - WINDOW_MS;
    // Defensive re-window even though the server already only keeps ~5
    // minutes - a clock skew or a slightly-stale cached response
    // shouldn't stretch the x-axis oddly.
    const samples = (series || []).filter((s) => s.t >= windowStart - 30000);
    const values = samples.filter((s) => s.v != null).map((s) => s.v);

    wrapEl.classList.remove("chart-empty");

    if (values.length === 0) {
      wrapEl.innerHTML = "";
      wrapEl.classList.add("chart-empty");
      wrapEl.textContent = "keine Daten";
      if (captionEl) captionEl.textContent = "";
      return;
    }

    const W = 300;
    const H = 60;
    const PAD = 4;
    const dataMin = Math.min(...values);
    const dataMax = Math.max(...values);
    let scaleMin = dataMin;
    let scaleMax = dataMax;
    if (scaleMin === scaleMax) {
      // Flat line (or a single sample) - widen the scale so it renders
      // as a visible flat line at mid-height instead of collapsing.
      scaleMin -= Math.max(1, Math.abs(scaleMin) * 0.1);
      scaleMax += Math.max(1, Math.abs(scaleMax) * 0.1);
    }

    const x = (t) => {
      const frac = Math.max(0, Math.min(1, (t - windowStart) / WINDOW_MS));
      return PAD + frac * (W - PAD * 2);
    };
    const y = (v) => {
      const frac = (v - scaleMin) / (scaleMax - scaleMin);
      return H - PAD - frac * (H - PAD * 2);
    };

    // Break the line at gaps (v === null) rather than drawing straight
    // through a disconnect as if the value held steady.
    const segments = [];
    let current = [];
    for (const s of samples) {
      if (s.v == null) {
        if (current.length) segments.push(current);
        current = [];
      } else {
        current.push(s);
      }
    }
    if (current.length) segments.push(current);

    const parts = segments.map((seg) => {
      if (seg.length === 1) {
        const cx = x(seg[0].t).toFixed(1);
        const cy = y(seg[0].v).toFixed(1);
        return `<circle cx="${cx}" cy="${cy}" r="1.8" class="chart-dot ${colorClass}"/>`;
      }
      const pts = seg.map((s) => `${x(s.t).toFixed(1)},${y(s.v).toFixed(1)}`).join(" ");
      return `<polyline points="${pts}" class="chart-line ${colorClass}"/>`;
    });

    wrapEl.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${parts.join("")}</svg>`;

    const baseCaption = (() => {
      const last = values[values.length - 1];
      const rangeText =
        dataMin === dataMax
          ? `${dataMin.toFixed(decimals)}${unit}`
          : `${dataMin.toFixed(decimals)}–${dataMax.toFixed(decimals)}${unit}`;
      return `aktuell ${last.toFixed(decimals)}${unit} · letzte 5 Min.: ${rangeText}`;
    })();
    if (captionEl) captionEl.textContent = baseCaption;

    // Point-under-cursor readout: hover (or touch-drag) shows the exact
    // value + time of the nearest sample instead of just the "current /
    // 5-min range" summary -- CONCEPT.md issue #2: stats should be "more
    // interactive". Deliberately modest: no zoom/pan, just a readout,
    // since this is a glance-at-a-chart tool next to a lighting desk, not
    // a full analytics view.
    const withValues = samples.filter((s) => s.v != null);
    if (withValues.length > 0) {
      attachHoverReadout(wrapEl, captionEl, withValues, x, y, W, H, unit, decimals, colorClass, baseCaption);
    }
  }

  function attachHoverReadout(wrapEl, captionEl, withValues, x, y, W, H, unit, decimals, colorClass, baseCaption) {
    const svg = wrapEl.querySelector("svg");
    if (!svg) return;

    const ns = "http://www.w3.org/2000/svg";
    const line = document.createElementNS(ns, "line");
    line.setAttribute("class", "chart-hover-line");
    line.setAttribute("y1", "0");
    line.setAttribute("y2", String(H));
    line.setAttribute("opacity", "0");
    const dot = document.createElementNS(ns, "circle");
    dot.setAttribute("class", "chart-hover-dot " + colorClass);
    dot.setAttribute("r", "2.4");
    dot.setAttribute("opacity", "0");
    svg.appendChild(line);
    svg.appendChild(dot);

    function nearestSample(clientX) {
      const rect = svg.getBoundingClientRect();
      if (rect.width === 0) return null;
      const frac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      const targetX = frac * W;
      let best = withValues[0];
      let bestDist = Math.abs(x(best.t) - targetX);
      for (const s of withValues) {
        const dist = Math.abs(x(s.t) - targetX);
        if (dist < bestDist) {
          best = s;
          bestDist = dist;
        }
      }
      return best;
    }

    function show(clientX) {
      const sample = nearestSample(clientX);
      if (!sample) return;
      const sx = x(sample.t).toFixed(1);
      const sy = y(sample.v).toFixed(1);
      line.setAttribute("x1", sx);
      line.setAttribute("x2", sx);
      line.setAttribute("opacity", "1");
      dot.setAttribute("cx", sx);
      dot.setAttribute("cy", sy);
      dot.setAttribute("opacity", "1");
      if (captionEl) {
        const d = new Date(sample.t);
        const hh = String(d.getHours()).padStart(2, "0");
        const mm = String(d.getMinutes()).padStart(2, "0");
        const ss = String(d.getSeconds()).padStart(2, "0");
        captionEl.textContent = `${sample.v.toFixed(decimals)}${unit} um ${hh}:${mm}:${ss}`;
      }
    }

    function hide() {
      line.setAttribute("opacity", "0");
      dot.setAttribute("opacity", "0");
      if (captionEl) captionEl.textContent = baseCaption;
    }

    svg.addEventListener("pointermove", (ev) => show(ev.clientX));
    svg.addEventListener("pointerdown", (ev) => show(ev.clientX));
    svg.addEventListener("pointerleave", hide);
  }

  return { WINDOW_MS, toSeries, renderSparkline };
})();
