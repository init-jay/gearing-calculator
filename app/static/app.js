/*
 * Gearing calculator frontend.
 *
 * All drivetrain math lives in calc.py and runs in the browser under Pyodide;
 * this file only gathers inputs, calls into Python, and draws SVG.
 *
 * Two drivetrains can be entered at once. Setup A is the reference; setup B
 * exists only while comparison is on. Everything downstream — gauges, chart,
 * tables — takes a list of setups, which is one entry long in the normal case.
 */

const MAX_GEARS = 8;
const MIN_GEARS = 1;
const MAX_CURVE_POINTS = 12;
const MIN_CURVE_POINTS = 2;

const SETUP_KEYS = ["a", "b"];

/**
 * Gearbox and engine presets from presets.json, whose first entry of each seeds
 * a fresh setup — so the defaults live in the data file, not in two places.
 * Preset torque curves are always N·m and are restated when imperial is selected.
 */
let presets = { gearboxes: [], engines: [] };

const $ = (sel) => document.querySelector(sel);

let pyodide = null;
let pyCompute = null;
let pyAtSpeed = null;
let pyTireDiameter = null;
let pyPowerAtRpm = null;
let pyConvertCurve = null;

/** Latest Python result per active setup key, e.g. `{a: {...}, b: {...}}`. */
let results = {};
/** Setup B is seeded from A the first time comparison is switched on. */
let seededB = false;

const comparing = () => $("#compare").checked;
const activeKeys = () => (comparing() ? SETUP_KEYS : ["a"]);
const setupRoot = (key) => $(`#setup-${key}`);

/** Top speed of the fastest active setup — the speed slider's ceiling. */
const topSpeed = () => Math.max(0, ...Object.values(results).map((r) => r.max_speed));

/* ------------------------------- geometry -------------------------------- */

/** Point on a circle. `angleDeg` is 0 at 12 o'clock, increasing clockwise. */
function polar(cx, cy, r, angleDeg) {
  const a = ((angleDeg - 90) * Math.PI) / 180;
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
}

/** SVG arc path from `startAngle` to `endAngle` (clockwise). */
function arcPath(cx, cy, r, startAngle, endAngle) {
  const [x1, y1] = polar(cx, cy, r, startAngle);
  const [x2, y2] = polar(cx, cy, r, endAngle);
  const largeArc = endAngle - startAngle > 180 ? 1 : 0;
  return `M ${x1} ${y1} A ${r} ${r} 0 ${largeArc} 1 ${x2} ${y2}`;
}

const svgEl = (name, attrs = {}, text = null) => {
  const el = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text !== null) el.textContent = text;
  return el;
};

/* -------------------------------- gauges --------------------------------- */

const GAUGE_START = -135; // lower-left
const GAUGE_END = 135; // lower-right
const GAUGE_SWEEP = GAUGE_END - GAUGE_START;

/**
 * Pick a round tick `step` and an upper bound >= `rawMax` that is a multiple of
 * it, so gauge ticks land on readable numbers (0/50/100…) rather than 0/29/58…
 */
function gaugeScale(rawMax, maxTicks = 7) {
  if (!(rawMax > 0)) return { max: 1, step: 1 };
  const mags = [1, 2, 2.5, 5, 10];
  const startExp = Math.floor(Math.log10(rawMax)) - 1;
  for (let e = startExp; e <= startExp + 3; e++) {
    for (const m of mags) {
      const step = m * Math.pow(10, e);
      const ticks = Math.ceil(rawMax / step);
      if (ticks >= 3 && ticks <= maxTicks) return { max: step * ticks, step };
    }
  }
  return { max: rawMax, step: rawMax / 5 };
}

/**
 * A tachometer counts in 1000-rpm majors and its dial *ends* at the redline, so
 * the red band is exactly the last major segment. Using `gaugeScale` here would
 * round the dial past the redline (9000 rpm → a 10000 dial) and pick a step the
 * band can't line up with. Redlines too low to divide into thousands fall back
 * to the generic scale, where the band is that scale's last step instead.
 */
function rpmScale(maxRpm) {
  const step = maxRpm >= 3000 ? 1000 : gaugeScale(maxRpm).step;
  return { max: Math.ceil(maxRpm / step) * step, step };
}

// One minor tick at each half-step, as on the reference cluster. Retune here.
const MINOR_PER_MAJOR = 2;

// Pulls the redline arc's ends off the two major ticks bounding it, so the red
// sits between them rather than running underneath.
const REDLINE_INSET_DEG = 2.5;

const R_BEZEL = 97;
const R_FACE = 95;
const R_TICK = 88; // outer edge of the tick ring; ticks grow inward from here
const R_MAJOR_IN = 76;
const R_MINOR_IN = 81;
const R_RED = 84; // centreline of the redline arc, spanning the tick ring's width
const R_LABEL = 62;
const R_NEEDLE = 70;
const R_HUB = 13;
const NEEDLE_TAIL = 9; // counterweight stub, kept shorter than the hub that hides it

/** A radial line from `r1` out to `r2` at `angle`. */
function spoke(cls, cx, cy, r1, r2, angle) {
  const [x1, y1] = polar(cx, cy, r1, angle);
  const [x2, y2] = polar(cx, cy, r2, angle);
  return svgEl("line", { class: cls, x1, y1, x2, y2 });
}

/** Tapered needle: narrow at the tip, wide across the tail. */
function needlePolygon(cls, cx, cy, angleDeg) {
  // `u` runs along the needle, `p` across it.
  const rad = ((angleDeg - 90) * Math.PI) / 180;
  const [ux, uy] = [Math.cos(rad), Math.sin(rad)];
  const [px, py] = [-uy, ux];
  const pt = (along, across) =>
    `${cx + along * ux + across * px},${cy + along * uy + across * py}`;
  return svgEl("polygon", {
    class: cls,
    points: [
      pt(R_NEEDLE, 1.0),
      pt(R_NEEDLE, -1.0),
      pt(-NEEDLE_TAIL, -3.6),
      pt(-NEEDLE_TAIL, 3.6),
    ].join(" "),
  });
}

/**
 * Draw a round instrument dial: dark face, tick ring, numerals, tapered needles,
 * and `legend` printed below the hub. Like the real cluster it carries no digital
 * readout; the live rpm is shown between the dials instead.
 *
 * The face is one instrument — a single scale and a single red band, sized to the
 * higher of the compared setups — but it carries a `needles` entry per setup, so
 * the comparison is read as the gap between two needles on one dial rather than
 * two dials to look back and forth between. They are drawn back to front, so
 * `needles[0]` ends up on top.
 * `redlineAt` (a value, optional) marks the band from there to `max` in red.
 */
function renderGauge(svg, { needles, max, step, legend, redlineAt = null }) {
  const cx = 100;
  const cy = 100;
  const angleFor = (v) => GAUGE_START + (max > 0 ? v / max : 0) * GAUGE_SWEEP;
  const inRedline = (v) => redlineAt !== null && v >= redlineAt;

  svg.replaceChildren();

  // Both dials live in one document, so gradient ids must not collide.
  const faceId = `${svg.id}-face`;
  // Stop colors come from CSS: `var()` is unreliable in presentation attributes.
  const grad = svgEl("radialGradient", { id: faceId });
  grad.append(svgEl("stop", { class: "gauge-face-center", offset: "0" }));
  grad.append(svgEl("stop", { class: "gauge-face-edge", offset: "1" }));
  const defs = svgEl("defs");
  defs.append(grad);
  svg.append(defs);

  svg.append(svgEl("circle", { cx, cy, r: R_FACE, fill: `url(#${faceId})` }));
  svg.append(svgEl("circle", { class: "gauge-bezel", cx, cy, r: R_BEZEL }));

  const big = max >= 1000; // numerals in thousands to keep the dial uncluttered
  const majors = Math.round(max / step);

  const labels = Array.from({ length: majors + 1 }, (_, i) => {
    const shown = big ? (i * step) / 1000 : i * step;
    return Number.isInteger(shown) ? String(shown) : shown.toFixed(1);
  });

  // One dial shows "7", another "300". Size the numerals to the widest of them
  // so three-digit scales don't collide and single-digit ones aren't tiny.
  const widest = Math.max(...labels.map((s) => s.length));
  svg.dataset.numWidth = widest <= 1 ? "narrow" : widest <= 2 ? "medium" : "wide";

  for (let i = 0; i <= majors * MINOR_PER_MAJOR; i++) {
    const v = (i / MINOR_PER_MAJOR) * step;
    const angle = angleFor(v);

    if (i % MINOR_PER_MAJOR === 0) {
      svg.append(spoke("gauge-tick-major", cx, cy, R_MAJOR_IN, R_TICK, angle));
      const [lx, ly] = polar(cx, cy, R_LABEL, angle);
      svg.append(svgEl("text", { class: "gauge-tick-label", x: lx, y: ly }, labels[i / MINOR_PER_MAJOR]));
    } else if (!inRedline(v)) {
      // Minor ticks would collide with the red band, so the band gets only red.
      svg.append(spoke("gauge-tick-minor", cx, cy, R_MINOR_IN, R_TICK, angle));
    }
  }

  // One unbroken arc, inset at both ends so it sits between the major ticks
  // bounding the band rather than on top of them.
  if (redlineAt !== null && redlineAt < max) {
    const from = angleFor(redlineAt) + REDLINE_INSET_DEG;
    const to = angleFor(max) - REDLINE_INSET_DEG;
    if (to > from) {
      svg.append(svgEl("path", { class: "gauge-redline", d: arcPath(cx, cy, R_RED, from, to) }));
    }
  }

  legend.forEach((line, i) => {
    svg.append(svgEl("text", { class: "gauge-legend", x: cx, y: cy + 30 + i * 11 }, line));
  });

  // Reversed so the first entry — the reference setup — is drawn last, on top.
  [...needles].reverse().forEach(({ value, cls }) => {
    const clamped = Math.max(0, Math.min(value, max));
    svg.append(needlePolygon(cls, cx, cy, angleFor(clamped)));
  });

  // Drawn after the needles so their bases disappear beneath the hub cap.
  svg.append(svgEl("circle", { class: "gauge-hub", cx, cy, r: R_HUB }));
}

/* --------------------------------- chart --------------------------------- */

/** Round `x` up to a "nice" 1/2/5 * 10^n value, for readable axis ticks. */
function niceStep(x) {
  if (x <= 0) return 1;
  const exp = Math.floor(Math.log10(x));
  const base = Math.pow(10, exp);
  const f = x / base;
  const nice = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
  return nice * base;
}

/**
 * Clear `svg`, draw its grid, axes and titles, and return the value→pixel maps.
 *
 * `y2Max` adds a right-hand axis for a second quantity sharing the plot box. It
 * gets tick marks and labels but no gridlines: its nice steps land at different
 * heights from the left axis's, and two interleaved grids read as noise.
 */
function drawFrame(svg, { W, H, M, xMax, yMax, xTitle, yTitle, y2Max = 0, y2Title = "" }) {
  const plotW = W - M.left - M.right;
  const plotH = H - M.top - M.bottom;
  const right = W - M.right;
  const bottom = M.top + plotH;

  const xPix = (v) => M.left + (v / xMax) * plotW;
  const yPix = (v) => bottom - (v / yMax) * plotH;
  const y2Pix = (v) => bottom - (v / y2Max) * plotH;

  svg.replaceChildren();

  const yStep = niceStep(yMax / 5);
  for (let v = 0; v <= yMax; v += yStep) {
    const y = yPix(v);
    svg.append(svgEl("line", { class: "chart-grid", x1: M.left, y1: y, x2: right, y2: y }));
    svg.append(
      svgEl("text", { class: "chart-label", x: M.left - 8, y: y + 4, "text-anchor": "end" },
        String(Math.round(v)))
    );
  }

  const xStep = niceStep(xMax / 6);
  for (let v = 0; v <= xMax; v += xStep) {
    const x = xPix(v);
    svg.append(svgEl("line", { class: "chart-grid", x1: x, y1: M.top, x2: x, y2: bottom }));
    svg.append(
      svgEl("text", { class: "chart-label", x, y: bottom + 18, "text-anchor": "middle" },
        String(Math.round(v)))
    );
  }

  svg.append(svgEl("line", { class: "chart-axis", x1: M.left, y1: M.top, x2: M.left, y2: bottom }));
  svg.append(svgEl("line", { class: "chart-axis", x1: M.left, y1: bottom, x2: right, y2: bottom }));
  svg.append(
    svgEl("text", { class: "chart-axis-title", x: M.left + plotW / 2, y: H - 6, "text-anchor": "middle" },
      xTitle)
  );
  svg.append(
    svgEl("text", {
      class: "chart-axis-title", x: 14, y: M.top + plotH / 2,
      "text-anchor": "middle", transform: `rotate(-90 14 ${M.top + plotH / 2})`,
    }, yTitle)
  );

  if (y2Max > 0) {
    svg.append(svgEl("line", { class: "chart-axis", x1: right, y1: M.top, x2: right, y2: bottom }));
    const step = niceStep(y2Max / 5);
    for (let v = 0; v <= y2Max; v += step) {
      const y = y2Pix(v);
      svg.append(svgEl("line", { class: "chart-axis", x1: right, y1: y, x2: right + 4, y2: y }));
      svg.append(
        svgEl("text", { class: "chart-label", x: right + 8, y: y + 4, "text-anchor": "start" },
          String(Math.round(v)))
      );
    }
    svg.append(
      svgEl("text", {
        class: "chart-axis-title", x: W - 6, y: M.top + plotH / 2,
        "text-anchor": "middle", transform: `rotate(-90 ${W - 6} ${M.top + plotH / 2})`,
      }, y2Title)
    );
  }

  return { xPix, yPix, y2Pix, plotW, plotH, top: M.top, bottom };
}

/**
 * Plot every setup's acceleration run against road speed.
 *
 * With one setup the gear curves are drawn behind the trace as context. With two
 * there would be a dozen of them, and the comparison — how the two runs differ —
 * is carried entirely by the traces, so the curves are dropped.
 */
function renderChart(svg, series, speed) {
  const compare = series.length > 1;
  const { W, H, M, X_HEADROOM } = CHART;

  // Road speed runs along X because it increases monotonically through a run,
  // so the shift trace reads left-to-right as a sawtooth. Both axes span the
  // union of the setups, so the two runs share one frame of reference.
  const xMax = Math.max(...series.map((s) => s.result.max_speed)) * X_HEADROOM || 1;
  const yMax = Math.max(...series.map((s) => s.result.max_rpm));

  const { xPix, yPix } = drawFrame(svg, {
    W, H, M, xMax, yMax,
    xTitle: `Speed (${series[0].result.speed_unit})`,
    yTitle: "Engine RPM",
  });

  // One polyline per gear. They share a color, so each is named where it ends —
  // at max RPM, spread along the top edge by the gear's top speed.
  if (!compare) {
    series[0].result.curves.forEach((curve) => {
      const pts = curve.samples.map(([rpm, spd]) => `${xPix(spd)},${yPix(rpm)}`).join(" ");
      svg.append(svgEl("polyline", { class: "chart-line", points: pts }));
      svg.append(
        svgEl("text", {
          class: "chart-gear-label", x: xPix(curve.top_speed), y: M.top - 6, "text-anchor": "middle",
        }, String(curve.gear))
      );
    });
  }

  // The acceleration run: up each gear to the shift RPM, then straight down to
  // the next gear at the same road speed.
  series.forEach((s) => {
    if (!s.result.trace.length) return;
    const pts = s.result.trace.map(([rpm, spd]) => `${xPix(spd)},${yPix(rpm)}`).join(" ");
    svg.append(svgEl("polyline", { class: `chart-trace chart-trace-${s.key}`, points: pts }));
    s.result.shifts.forEach((shift) => {
      svg.append(
        svgEl("circle", {
          class: "chart-shift", cx: xPix(shift.speed), cy: yPix(s.result.shift_rpm), r: 3,
        })
      );
    });
  });

  // Marker for the current speed, in whichever gear the shift schedule puts it.
  // A setup that cannot reach this speed would need more rpm than it has, which
  // puts its marker off the top of the plot; it is dropped rather than clamped.
  series.forEach((s) => {
    if (speed < 0 || speed > xMax || s.at.rpm > yMax) return;
    svg.append(
      svgEl("circle", {
        class: `chart-marker chart-marker-${s.key}`, cx: xPix(speed), cy: yPix(s.at.rpm), r: 4,
      })
    );
  });
}

/** The gear curves are named on the plot itself, so only the traces need a key. */
function renderLegend(el, series) {
  el.replaceChildren();
  const compare = series.length > 1;

  series.forEach((s) => {
    if (!s.result.shifts.length) return;
    const span = document.createElement("span");
    const sw = document.createElement("i");
    sw.className = `swatch swatch-trace swatch-trace-${s.key}`;
    const name = compare ? `Setup ${s.label} trace` : "Shift trace";
    span.append(sw, document.createTextNode(`${name} (@ ${Math.round(s.result.shift_rpm)} rpm)`));
    el.append(span);
  });

  if (compare) {
    const note = document.createElement("span");
    note.className = "legend-note";
    note.textContent = "Gear curves hidden while comparing";
    el.append(note);
  }
}

/* --------------------------- rpm-vs-speed chart -------------------------- */

/**
 * Geometry of the RPM-vs-Speed plot. Shared with the gear-spread bar above it so
 * the two line up: both are drawn at the same width and origin, and the bar's
 * full width is the same road speed as the chart's rightmost gridline region.
 * `M.top` clears the gear numbers printed above where each curve ends.
 */
const CHART = {
  W: 640,
  H: 360,
  M: { top: 24, right: 16, bottom: 44, left: 52 },
  X_HEADROOM: 1.05, // a little air past the top speed, so the trace's end is visible
};

/* ------------------------------ gear spread ------------------------------ */

// Amber for setup A, blue for B, as everywhere else. Successive gears fade so a
// six-speed's bands stay tellable apart beyond the separators between them.
const SPREAD_RGB = { a: "245, 161, 29", b: "59, 154, 225" };
const SPREAD_MIN_ALPHA = 0.45;

// Below this share a band is too narrow for its percentage; the gear number and
// the `title` still fit.
const SPREAD_LABEL_SHARE = 0.08;

/**
 * A bar showing which slice of 0-to-top-speed each gear covers.
 *
 * Bars are drawn against the *union* top speed rather than each setup's own, so
 * a slower setup's bar ends short and the two can be read against each other —
 * and against the speed axis of the chart directly below.
 */
function renderSpread(el, series) {
  el.replaceChildren();
  const compare = series.length > 1;
  const unit = series[0].result.speed_unit;
  const top = Math.max(...series.map((s) => s.result.max_speed));
  if (!(top > 0)) return;

  // Inset the bars to the chart's plot box, and end them where the chart puts the
  // top speed, so a gear's band sits directly over the speeds it covers below.
  const pct = (v) => `${(v / CHART.W) * 100}%`;
  const plotW = CHART.W - CHART.M.left - CHART.M.right;
  el.style.setProperty("--plot-left", pct(CHART.M.left));
  el.style.setProperty("--plot-width", pct(plotW / CHART.X_HEADROOM));

  const caption = document.createElement("p");
  caption.className = "spread-caption";
  caption.textContent = `Share of 0–${Math.round(top)} ${unit} covered by each gear`;
  el.append(caption);

  series.forEach((s) => {
    const row = document.createElement("div");
    row.className = "spread-row";

    if (compare) {
      const tag = document.createElement("span");
      tag.className = `spread-tag spread-tag-${s.key}`;
      tag.textContent = s.label;
      row.append(tag);
    }

    const bar = document.createElement("div");
    bar.className = "spread-bar";
    const gears = s.result.spread.length;

    s.result.spread.forEach((span, i) => {
      const seg = document.createElement("div");
      seg.className = "spread-seg";
      // Widths are a share of the union top speed, not of this setup's own, so
      // the bar of a slower setup genuinely stops short of the full width.
      seg.style.flex = `0 0 ${((span.to_speed - span.from_speed) / top) * 100}%`;
      const fade = gears > 1 ? i / (gears - 1) : 0;
      seg.style.background = `rgba(${SPREAD_RGB[s.key]}, ${1 - (1 - SPREAD_MIN_ALPHA) * fade})`;

      const pct = Math.round(span.share * 100);
      seg.title =
        `Gear ${span.gear}: ${span.from_speed.toFixed(1)}–${span.to_speed.toFixed(1)} ${unit}` +
        ` (${pct}% of the range)`;
      seg.textContent = span.share >= SPREAD_LABEL_SHARE ? `${span.gear} · ${pct}%` : String(span.gear);
      bar.append(seg);
    });

    row.append(bar);
    el.append(row);
  });
}

/* ---------------------------- tractive effort ---------------------------- */

const lastSample = (curve) => curve.samples[curve.samples.length - 1];

/**
 * Force at the contact patch against road speed, one curve per gear.
 *
 * This is the plot that makes the torque-vs-power argument concrete: every curve
 * is the same engine torque scaled by a different ratio, and the point where one
 * gear's curve dives under the next gear's is the upshift that keeps the most
 * force under you. Those crossings are marked.
 */
function renderEffortChart(svg, series, speed) {
  const W = 640;
  const H = 360;
  const M = { top: 24, right: 16, bottom: 44, left: 62 };

  // The curves stop where the torque curve does, which may be short of redline.
  const xMax = Math.max(...series.flatMap((s) => s.result.efforts.map((e) => lastSample(e)[0]))) * 1.05;
  const yMax = Math.max(...series.map((s) => s.result.max_force)) * 1.05;

  const { xPix, yPix } = drawFrame(svg, {
    W, H, M, xMax, yMax,
    xTitle: `Speed (${series[0].result.speed_unit})`,
    yTitle: `Tractive effort (${series[0].result.force_unit})`,
  });

  series.forEach((s) => {
    s.result.efforts.forEach((curve) => {
      const pts = curve.samples.map(([spd, force]) => `${xPix(spd)},${yPix(force)}`).join(" ");
      svg.append(svgEl("polyline", { class: `chart-effort chart-effort-${s.key}`, points: pts }));

      // Named where it ends — the curves fan out towards higher speeds, so their
      // tails are the only place a gear number is unambiguous. B's sit below A's.
      const [spd, force] = lastSample(curve);
      svg.append(
        svgEl("text", {
          class: `chart-effort-label chart-effort-label-${s.key}`,
          x: xPix(spd), y: yPix(force) + (s.key === "b" ? 14 : -6), "text-anchor": "middle",
        }, String(curve.gear))
      );
    });

    s.result.crossovers.forEach((cross) => {
      if (cross.at_redline) return; // nothing to mark: the curves never met
      svg.append(
        svgEl("circle", {
          class: "chart-cross", cx: xPix(cross.speed), cy: yPix(cross.force), r: 4,
        })
      );
    });

    // Python's `None` arrives as `undefined`, not `null`. There is no force to
    // plot when the current speed puts the engine outside the curve's rev range.
    if (Number.isFinite(s.at.force) && speed <= xMax) {
      svg.append(
        svgEl("circle", {
          class: `chart-marker chart-marker-${s.key}`, cx: xPix(speed), cy: yPix(s.at.force), r: 4,
        })
      );
    }
  });
}

/**
 * Torque and its derived power against engine speed, on twinned axes.
 *
 * The markers are the engine's operating point at the current road speed, one on
 * each curve — so dragging the speed slider walks them along the torque and power
 * lines together. The peaks are named in the caption instead; a static dot and a
 * live dot on the same curve read as the same thing.
 */
function renderEngineChart(svg, series) {
  const W = 640;
  const H = 300;
  const M = { top: 16, right: 56, bottom: 44, left: 56 };

  const xMax = Math.max(...series.map((s) => s.result.max_rpm));
  const yMax = Math.max(...series.flatMap((s) => s.result.engine.map((e) => e[1]))) * 1.15;
  const y2Max = Math.max(...series.flatMap((s) => s.result.engine.map((e) => e[2]))) * 1.15;

  const { xPix, yPix, y2Pix } = drawFrame(svg, {
    W, H, M, xMax, yMax, y2Max,
    xTitle: "Engine RPM",
    yTitle: `Torque (${series[0].result.torque_unit})`,
    y2Title: `Power (${series[0].result.power_unit})`,
  });

  series.forEach((s) => {
    const torque = s.result.engine.map(([rpm, t]) => `${xPix(rpm)},${yPix(t)}`).join(" ");
    const power = s.result.engine.map(([rpm, , p]) => `${xPix(rpm)},${y2Pix(p)}`).join(" ");
    svg.append(svgEl("polyline", { class: `chart-torque chart-torque-${s.key}`, points: torque }));
    svg.append(svgEl("polyline", { class: `chart-power chart-power-${s.key}`, points: power }));
  });

  // Drawn after every curve so a marker is never buried under the other setup's
  // line. `None` arrives from Python as `undefined`: the engine is off the curve.
  series.forEach((s) => {
    if (!Number.isFinite(s.at.torque)) return;
    const x = xPix(s.at.rpm);
    svg.append(svgEl("circle", { class: `chart-marker chart-marker-${s.key}`, cx: x, cy: yPix(s.at.torque), r: 4 }));
    svg.append(svgEl("circle", { class: `chart-marker chart-marker-${s.key}`, cx: x, cy: y2Pix(s.at.power), r: 4 }));
  });
}

/** Swatch + text, the shape every legend entry takes. */
function legendEntry(el, cls, text) {
  const span = document.createElement("span");
  const sw = document.createElement("i");
  sw.className = cls;
  span.append(sw, document.createTextNode(text));
  el.append(span);
}

function renderEffortLegend(el, series) {
  el.replaceChildren();
  const compare = series.length > 1;
  series.forEach((s) => {
    legendEntry(el, `swatch swatch-effort-${s.key}`, compare ? `Setup ${s.label} gears` : "Gear curves");
  });
  if (series.some((s) => s.result.crossovers.some((c) => !c.at_redline))) {
    legendEntry(el, "swatch swatch-dot", "Optimal shift point");
  }
}

/** Peak torque and peak power, spelled out under the engine chart. */
function renderPeaks(el, series) {
  el.replaceChildren();
  const compare = series.length > 1;
  series.forEach((s) => {
    const r = s.result;
    const [tRpm, tVal] = r.peak_torque;
    const [pRpm, pVal] = r.peak_power;
    const who = compare ? `Setup ${s.label}: ` : "";
    const span = document.createElement("span");
    span.textContent =
      `${who}peak torque ${tVal.toFixed(0)} ${r.torque_unit} @ ${Math.round(tRpm)} rpm` +
      ` · peak power ${pVal.toFixed(1)} ${r.power_unit} @ ${Math.round(pRpm)} rpm`;
    el.append(span);
  });
}

/* --------------------------------- table --------------------------------- */

const cellEl = (tag, text, attrs = {}) => {
  const el = document.createElement(tag);
  el.textContent = text;
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, v);
  return el;
};

/**
 * Render a table whose value columns each split into one sub-column per setup
 * while comparing, so A's third gear sits beside B's rather than a row below it.
 *
 * `spec.rows(s)` is how many rows that setup contributes; the table takes the
 * longest, and a setup with fewer gears gets an em dash. Rows are keyed by index
 * — gear 3 is gear 3 in both setups — which is what makes them comparable at all.
 *
 *   spec.key   { label, of(i) }        the leading, unsplit column
 *   spec.cols  [{ label(result), of(s, i), current?(s, i) }]
 */
function renderCompareTable(table, series, spec) {
  const compare = series.length > 1;
  const head = table.tHead;
  const body = table.tBodies[0];
  head.replaceChildren();
  body.replaceChildren();

  const top = document.createElement("tr");
  top.append(cellEl("th", spec.key.label, { scope: "col", rowspan: compare ? 2 : null }));
  for (const col of spec.cols) {
    top.append(
      cellEl("th", col.label(series[0].result), {
        class: "group-start",
        scope: compare ? "colgroup" : "col",
        colspan: compare ? series.length : null,
      })
    );
  }
  head.append(top);

  if (compare) {
    // The setup letters, tinted to match each setup's colour on the charts.
    const sub = document.createElement("tr");
    for (const col of spec.cols) {
      series.forEach((s, i) => {
        sub.append(cellEl("th", s.label, { scope: "col", class: `sub sub-${s.key}${i === 0 ? " group-start" : ""}` }));
      });
    }
    head.append(sub);
  }

  const rows = Math.max(...series.map(spec.rows));
  for (let i = 0; i < rows; i++) {
    const tr = document.createElement("tr");
    tr.append(cellEl("th", spec.key.of(i), { scope: "row" }));

    // With one setup the whole row is "the gear you are in". With two, each is in
    // its own gear, so only the cells of the setup that is in this one light up.
    if (!compare && spec.cols.some((c) => c.current?.(series[0], i))) tr.className = "is-current";

    for (const col of spec.cols) {
      series.forEach((s, n) => {
        const value = col.of(s, i);
        const classes = [
          n === 0 ? "group-start" : "",
          compare && col.current?.(s, i) ? "is-current" : "",
        ];
        tr.append(cellEl("td", value ?? "—", { class: classes.filter(Boolean).join(" ") || null }));
      });
    }
    body.append(tr);
  }
}

function renderTable(table, series) {
  renderCompareTable(table, series, {
    key: { label: "Gear", of: (i) => String(i + 1) },
    rows: (s) => s.result.curves.length,
    cols: [
      {
        label: () => "Ratio",
        of: (s, i) => s.result.curves[i]?.ratio.toFixed(3),
        current: (s, i) => s.result.curves[i]?.gear === s.at.gear,
      },
      {
        label: () => "Overall",
        of: (s, i) => {
          const curve = s.result.curves[i];
          return curve && (curve.ratio * s.inputs.final_drive * s.inputs.transfer).toFixed(3);
        },
        current: (s, i) => s.result.curves[i]?.gear === s.at.gear,
      },
      {
        label: (r) => `Top speed (${r.speed_unit})`,
        of: (s, i) => s.result.curves[i]?.top_speed.toFixed(1),
        current: (s, i) => s.result.curves[i]?.gear === s.at.gear,
      },
    ],
  });
}

/** One row per upshift: where the engine lands in the next gear. */
function renderShiftTable(table, series) {
  renderCompareTable(table, series, {
    key: { label: "Shift", of: (i) => `${i + 1} → ${i + 2}` },
    rows: (s) => s.result.shifts.length,
    cols: [
      { label: (r) => `Speed (${r.speed_unit})`, of: (s, i) => s.result.shifts[i]?.speed.toFixed(1) },
      { label: () => "RPM after", of: (s, i) => s.result.shifts[i]?.rpm_after.toFixed(1) },
      { label: () => "RPM drop", of: (s, i) => rounded(s.result.shifts[i]?.rpm_drop) },
    ],
  });
}

/** `Math.round` that survives the missing row of a setup with fewer gears. */
const rounded = (v) => (v == null ? undefined : String(Math.round(v)));

/** Optimal upshifts, from the tractive-effort crossovers rather than a fixed RPM. */
function renderCrossTable(table, series) {
  renderCompareTable(table, series, {
    key: { label: "Shift", of: (i) => `${i + 1} → ${i + 2}` },
    rows: (s) => s.result.crossovers.length,
    cols: [
      { label: (r) => `Speed (${r.speed_unit})`, of: (s, i) => s.result.crossovers[i]?.speed.toFixed(1) },
      {
        label: () => "Upshift at",
        of: (s, i) => {
          const cross = s.result.crossovers[i];
          if (!cross) return undefined;
          // A pair that never crosses is held to the limiter; say so rather than
          // print the redline as though it were a computed optimum.
          return cross.at_redline ? `${Math.round(cross.rpm)} (redline)` : String(Math.round(cross.rpm));
        },
      },
      { label: () => "Lands at", of: (s, i) => rounded(s.result.crossovers[i]?.rpm_after) },
      { label: (r) => `Force (${r.force_unit})`, of: (s, i) => s.result.crossovers[i]?.force.toFixed(0) },
    ],
  });
}

/* --------------------------------- inputs -------------------------------- */

const field = (root, name) => root.querySelector(`[data-field="${name}"]`);
const gearInputs = (root) => [...root.querySelectorAll(".gear-input")];
const curveRows = (root) => [...root.querySelectorAll(".curve-row")];

const currentUnits = () => document.querySelector('input[name="units"]:checked').value;

const num = (root, name, fallback) => {
  const v = parseFloat(field(root, name).value);
  return Number.isFinite(v) ? v : fallback;
};

/** Fill a setup panel from the shared form template, namespacing its ids. */
function buildSetupForm(key) {
  const frag = $("#setup-template").content.cloneNode(true);
  const labels = [...frag.querySelectorAll("label[for]")];
  frag.querySelectorAll("[id]").forEach((el) => (el.id = `${key}-${el.id}`));
  labels.forEach((el) => (el.htmlFor = `${key}-${el.htmlFor}`));
  setupRoot(key).append(frag);

  const root = setupRoot(key);
  fillPresetSelect(presetSelect(root, "gearbox"), presets.gearboxes);
  fillPresetSelect(presetSelect(root, "engine"), presets.engines);
}

/**
 * Derive the tire diameter from the 225/45R17-style size inputs and write it
 * into the read-only `tire` field, in whichever unit is currently selected.
 * Must run before `readInputs`, which treats `tire` as the source of truth.
 */
function updateTireDiameter(root) {
  const units = currentUnits();
  const dia = pyTireDiameter(
    num(root, "tire_width", 225), num(root, "tire_aspect", 45), num(root, "tire_wheel", 17), units
  );
  field(root, "tire").value = dia.toFixed(units === "metric" ? 1 : 2);
}

/** The entered torque curve as `[[rpm, torque], ...]`; Python sorts and cleans it. */
function readCurve(root) {
  return curveRows(root)
    .map((row) => [
      parseFloat(row.querySelector(".curve-rpm").value),
      parseFloat(row.querySelector(".curve-torque").value),
    ])
    .filter(([rpm, torque]) => Number.isFinite(rpm) && Number.isFinite(torque));
}

/**
 * Fill in each row's power cell. Power is torque times engine speed, so it is
 * shown rather than asked for — a row where you could type both would let you
 * describe an engine that cannot exist.
 */
function updatePowerCells(root) {
  const units = currentUnits();
  curveRows(root).forEach((row) => {
    const rpm = parseFloat(row.querySelector(".curve-rpm").value);
    const torque = parseFloat(row.querySelector(".curve-torque").value);
    const cell = row.querySelector(".curve-power");
    const ok = Number.isFinite(rpm) && Number.isFinite(torque) && rpm > 0 && torque > 0;
    cell.textContent = ok ? pyPowerAtRpm(torque, rpm, units).toFixed(1) : "—";
  });
}

function readInputs(root) {
  return {
    gears: gearInputs(root).map((el) => parseFloat(el.value)).filter((v) => Number.isFinite(v) && v > 0),
    final_drive: num(root, "final_drive", 3.64),
    transfer: num(root, "transfer", 1.0),
    tire: num(root, "tire", 634.3),
    slip: num(root, "slip", 0) / 100,
    max_rpm: num(root, "max_rpm", 7000),
    shift_rpm: num(root, "shift_rpm", 7000),
    units: currentUnits(),
    torque_curve: readCurve(root),
  };
}

function buildGearRows(root, ratios) {
  const list = root.querySelector("[data-gear-list]");
  list.replaceChildren();
  ratios.forEach((ratio, i) => {
    const row = document.createElement("div");
    row.className = "gear-row";

    const label = document.createElement("span");
    label.textContent = String(i + 1);

    const input = document.createElement("input");
    input.type = "number";
    input.className = "gear-input";
    input.step = "0.01";
    input.min = "0.01";
    input.value = String(ratio);
    input.setAttribute("aria-label", `Gear ${i + 1} ratio`);

    row.append(label, input);
    list.append(row);
  });
  syncGearButtons(root);
}

function syncGearButtons(root) {
  const n = gearInputs(root).length;
  root.querySelector("[data-add-gear]").disabled = n >= MAX_GEARS;
  root.querySelector("[data-remove-gear]").disabled = n <= MIN_GEARS;
}

function buildCurveRows(root, points) {
  const list = root.querySelector("[data-curve-list]");
  list.replaceChildren();
  points.forEach(([rpm, torque], i) => {
    const row = document.createElement("div");
    row.className = "curve-row";

    const rpmInput = document.createElement("input");
    rpmInput.type = "number";
    rpmInput.className = "curve-rpm";
    rpmInput.step = "100";
    rpmInput.min = "1";
    rpmInput.value = String(Math.round(rpm));
    rpmInput.setAttribute("aria-label", `Point ${i + 1} RPM`);

    const torqueInput = document.createElement("input");
    torqueInput.type = "number";
    torqueInput.className = "curve-torque";
    torqueInput.step = "0.1";
    torqueInput.min = "0.1";
    torqueInput.value = String(+torque.toFixed(1));
    torqueInput.setAttribute("aria-label", `Point ${i + 1} torque`);

    const power = document.createElement("span");
    power.className = "curve-power";

    row.append(rpmInput, torqueInput, power);
    list.append(row);
  });
  syncCurveButtons(root);
  updatePowerCells(root);
}

function syncCurveButtons(root) {
  const n = curveRows(root).length;
  root.querySelector("[data-add-point]").disabled = n >= MAX_CURVE_POINTS;
  root.querySelector("[data-remove-point]").disabled = n <= MIN_CURVE_POINTS;
}

/** Restate a torque curve in the other unit system, via Python's constant. */
function convertCurve(points, units) {
  const pyIn = pyodide.toPy(points);
  let proxy;
  try {
    proxy = pyConvertCurve(pyIn, units);
    return proxy.toJs();
  } finally {
    pyIn.destroy();
    if (proxy) proxy.destroy();
  }
}

/* -------------------------------- presets -------------------------------- */

const presetSelect = (root, kind) => root.querySelector(`[data-preset="${kind}"]`);

/** Options are indices into `presets`; the empty value means hand-entered. */
function fillPresetSelect(sel, items) {
  sel.replaceChildren(new Option("Custom", ""));
  items.forEach((item, i) => sel.append(new Option(item.name, String(i))));
}

/**
 * A preset names a set of numbers. Once any of those numbers is edited the name
 * is a lie, so every path that touches a ratio or a curve point clears it.
 */
function markCustom(root, kind) {
  presetSelect(root, kind).value = "";
}

function applyPreset(root, sel) {
  const chosen = presets[sel.dataset.preset === "gearbox" ? "gearboxes" : "engines"][sel.value];
  if (!chosen) return; // re-picking "Custom" keeps whatever is on screen

  if (sel.dataset.preset === "gearbox") {
    buildGearRows(root, chosen.gears);
  } else {
    // The redline belongs to the engine, so it comes along. Presets are metric.
    const curve = currentUnits() === "metric"
      ? chosen.torque_curve
      : convertCurve(chosen.torque_curve, "imperial");
    buildCurveRows(root, curve);
    field(root, "max_rpm").value = String(chosen.redline);
    // Keep an earlier shift point if the user chose one; never exceed the redline.
    const shift = num(root, "shift_rpm", chosen.redline);
    field(root, "shift_rpm").value = String(Math.min(shift, chosen.redline));
  }
  recompute();
}

/** Seed one setup from another, so comparison starts from a single changed field. */
function copySetup(from, to) {
  const src = setupRoot(from);
  const dst = setupRoot(to);
  buildGearRows(dst, gearInputs(src).map((el) => parseFloat(el.value) || 1));
  buildCurveRows(dst, readCurve(src));
  src.querySelectorAll("[data-field]").forEach((el) => {
    field(dst, el.dataset.field).value = el.value;
  });
  // Copy the preset names too, or B would read "Custom" while showing A's numbers.
  for (const kind of ["gearbox", "engine"]) {
    presetSelect(dst, kind).value = presetSelect(src, kind).value;
  }
}

/* ------------------------------ python bridge ---------------------------- */

/** Call a Python function with a JS object argument, converting both ways. */
function callPy(fn, inputs, ...rest) {
  const pyIn = pyodide.toPy(inputs);
  let proxy;
  try {
    proxy = fn(pyIn, ...rest);
    return proxy.toJs({ dict_converter: Object.fromEntries });
  } finally {
    pyIn.destroy();
    if (proxy) proxy.destroy();
  }
}

/** Recompute every active setup's gear curves via Python, then redraw. */
function recompute() {
  const next = {};
  for (const key of activeKeys()) {
    const root = setupRoot(key);
    updateTireDiameter(root);
    updatePowerCells(root);
    const inputs = readInputs(root);
    if (inputs.gears.length === 0) return; // mid-edit; keep the last good result

    // Advertise the ceiling, but don't rewrite the value here: `recompute` runs on
    // every keystroke, and a half-typed redline ("8" of "8000") would clobber it.
    // Python clamps the shift RPM to the redline anyway, so the math stays right;
    // `onShiftRpmCommit` tidies the field once the user is done typing.
    field(root, "shift_rpm").max = String(inputs.max_rpm);

    next[key] = callPy(pyCompute, inputs);
  }
  results = next;

  // The speed slider's ceiling is the fastest setup's top speed, so both runs are
  // reachable on one slider. `floor` keeps the slider inside the plot.
  const slider = $("#cur_speed");
  slider.max = String(Math.floor(topSpeed()));
  if (parseFloat(slider.value) > topSpeed()) slider.value = slider.max;

  // Table headers carry their own units, built from the result; this is the
  // slider's label, which is not.
  document.querySelectorAll("[data-speed-unit]").forEach((el) => {
    el.textContent = results.a.speed_unit;
  });

  redraw();
}

/** The per-setup bundle every renderer takes: inputs, Python result, current state. */
function buildSeries(speed) {
  const series = [];
  for (const key of activeKeys()) {
    if (!results[key]) return null;
    const inputs = readInputs(setupRoot(key));
    if (inputs.gears.length === 0) return null;
    series.push({
      key,
      label: key.toUpperCase(),
      inputs,
      result: results[key],
      // Python decides which gear the shift schedule puts us in, and the rpm that
      // gear needs to hold this speed — so the marker always lands on the trace.
      at: callPy(pyAtSpeed, inputs, speed),
    });
  }
  return series;
}

/** Redraw gauges/chart/tables from `results` + the current road speed. */
function redraw() {
  const speed = parseFloat($("#cur_speed").value);
  const series = buildSeries(speed);
  if (!series) return;

  const compare = series.length > 1;
  $("#cur_speed_out").textContent = String(Math.round(speed));
  $("#cur_gear").textContent = series.map((s) => s.at.gear).join(" / ");

  // One face for both setups: the scale spans the taller redline and the red band
  // is that dial's own last major segment, so the two needles are read against the
  // same markings. Two scales, or two red bands at different radii, would mean the
  // gap between the needles no longer stood for a difference in rpm.
  const tach = rpmScale(Math.max(...series.map((s) => s.inputs.max_rpm)));
  renderGauge($("#tach"), {
    needles: series.map((s) => ({ value: s.at.rpm, cls: `gauge-needle gauge-needle-${s.key}` })),
    ...tach,
    legend: tach.max >= 1000 ? ["1/min", "×1000"] : ["1/min"],
    redlineAt: tach.max - tach.step, // the last major segment, i.e. the top 1000 rpm
  });

  // Road speed is the shared independent variable — both setups are always at the
  // slider's speed — so a second speedo needle would land exactly on the first.
  // Only the scale grows, to cover the faster setup's top speed.
  const speedScale = gaugeScale(Math.max(1, ...series.map((s) => s.result.max_speed)));
  renderGauge($("#speedo"), {
    needles: [{ value: speed, cls: "gauge-needle" }],
    ...speedScale,
    legend: [series[0].result.speed_unit],
  });

  for (const s of series) {
    const row = $(`.rpm-row[data-setup="${s.key}"]`);
    row.querySelector("output").textContent = String(Math.round(s.at.rpm));
    // Either setup can be asked for a speed it cannot reach, which needs more rpm
    // than it has. Flag that rather than hide it — the needle merely pegs.
    row.toggleAttribute("data-over", s.at.rpm > s.inputs.max_rpm + 0.5);
  }

  renderSpread($("#spread"), series);
  renderChart($("#chart"), series, speed);
  renderLegend($("#legend"), series);
  renderTable($("#table"), series);
  renderShiftTable($("#shift-table"), series);

  // A curve shorter than two points, or one that starts above the redline, leaves
  // Python with nothing to plot. Hide the whole section rather than draw an empty
  // frame — and require *every* setup to have one, since the charts overlay them.
  const hasEngine = series.every((s) => s.result.efforts.length > 0);
  $("#effort-section").hidden = !hasEngine;
  if (hasEngine) {
    renderEffortChart($("#effort-chart"), series, speed);
    renderEffortLegend($("#effort-legend"), series);
    renderEngineChart($("#engine-chart"), series);
    renderPeaks($("#engine-peaks"), series);
    renderCrossTable($("#cross-table"), series);
  }
}

/* ------------------------------ runtime config --------------------------- */

/**
 * How to fix a failed boot. Empty until the runtime's source is known, because
 * until then the two failures — no vendored copy, no network — are indist-
 * inguishable, and guessing sends the reader to the wrong file.
 */
let runtimeHint = "";

/**
 * The base URL Pyodide and its wasm/stdlib payload are fetched from.
 *
 * `vendored` keeps every byte on this origin, which is what lets the site run
 * with no network. `cdn` trades that away for a deploy that carries no 12 MB
 * runtime. The version is substituted here rather than baked into the URL, so
 * the CDN cannot drift from the copy `scripts/vendor_pyodide.py` downloads.
 */
function pyodideBaseUrl(cfg) {
  if (cfg.source !== "vendored" && cfg.source !== "cdn") {
    throw new Error(`config.json: pyodide.source must be "vendored" or "cdn", got "${cfg.source}"`);
  }
  runtimeHint =
    cfg.source === "cdn"
      ? 'config.json loads Pyodide from a CDN. Check the network, or set <code>pyodide.source</code> to <code>"vendored"</code>.'
      : "Run <code>uv run scripts/vendor_pyodide.py</code> to fetch the runtime.";

  const base = cfg.source === "cdn" ? cfg.cdn.replace("{version}", cfg.version) : cfg.vendored;
  return base.endsWith("/") ? base : `${base}/`;
}

/** Pyodide ships as a classic script that defines `loadPyodide` on `window`. */
function loadScript(src) {
  return new Promise((resolve, reject) => {
    const el = document.createElement("script");
    el.src = src;
    el.onload = () => resolve();
    el.onerror = () => reject(new Error(`could not load ${src}`));
    document.head.append(el);
  });
}

/* ---------------------------------- init --------------------------------- */

/** Show one setup's form; the other stays in the DOM so its values persist. */
function selectTab(key) {
  for (const k of SETUP_KEYS) {
    const on = k === key;
    $(`#tab-${k}`).setAttribute("aria-selected", String(on));
    setupRoot(k).hidden = !on;
  }
}

function onCompareChange() {
  const on = comparing();
  if (on && !seededB) {
    // Start B as a copy of A: a comparison is only legible when one thing differs.
    copySetup("a", "b");
    seededB = true;
  }

  $("#setup-tabs").hidden = !on;
  $("#gear_scope").hidden = !on;
  $(".gauges").toggleAttribute("data-compare", on);
  $('.rpm-row[data-setup="b"]').hidden = !on;
  $('.rpm-row[data-setup="a"] .rpm-tag').hidden = !on;

  // Land on B when comparison opens: it is the form the user came here to fill in.
  selectTab(on ? "b" : "a");
  recompute();
}

function onUnitChange() {
  const slider = $("#cur_speed");
  const oldMax = topSpeed();
  const oldValue = parseFloat(slider.value);

  const metric = currentUnits() === "metric";

  // The tire size itself is unit-agnostic; `recompute` re-derives the diameter
  // in the newly selected unit, so only the label needs updating here.
  document.querySelectorAll("[data-tire-unit]").forEach((el) => {
    el.textContent = metric ? "mm" : "in";
  });
  document.querySelectorAll("[data-torque-unit]").forEach((el) => {
    el.textContent = metric ? "N·m" : "lb-ft";
  });
  document.querySelectorAll("[data-power-unit]").forEach((el) => {
    el.textContent = metric ? "kW" : "hp";
  });

  // Torque, unlike the tire size, has no unit-agnostic source to re-derive from:
  // the numbers on screen *are* the data, so they have to be restated in place.
  // Both setups convert, even the inactive one, or B would silently keep lb-ft.
  for (const key of SETUP_KEYS) {
    const root = setupRoot(key);
    buildCurveRows(root, convertCurve(readCurve(root), currentUnits()));
  }

  recompute();

  // Keep the slider on the same physical speed. Both the old and new ceilings
  // scale by the same factor across a unit change, so their ratio converts the
  // value exactly — no hardcoded 1.609344. Scale from the pre-clamp value, since
  // `recompute` may have pulled it down to the new (smaller) ceiling.
  if (oldMax > 0 && Number.isFinite(oldValue)) {
    const scaled = Math.round(oldValue * (topSpeed() / oldMax));
    slider.value = String(Math.min(scaled, parseFloat(slider.max)));
    redraw();
  }
}

/** Once the user commits a shift RPM, pull it back under that setup's redline. */
function onShiftRpmCommit(root) {
  const maxRpm = num(root, "max_rpm", 7000);
  if (num(root, "shift_rpm", maxRpm) > maxRpm) {
    field(root, "shift_rpm").value = String(maxRpm);
    recompute();
  }
}

function wireEvents() {
  // Delegated: both setup forms are cloned at boot and their gear rows are rebuilt
  // whenever a gear is added or removed, so nothing can hold a direct listener.
  const setups = $("#setups");
  setups.addEventListener("input", (e) => {
    const root = e.target.closest(".setup");
    if (e.target.dataset.preset) return; // a <select> fires both; `change` applies it
    if (root) {
      // The redline is part of the engine preset, so retyping it is a custom engine.
      if (e.target.classList.contains("gear-input")) markCustom(root, "gearbox");
      else if (e.target.closest("[data-curve-list]") || e.target.dataset.field === "max_rpm")
        markCustom(root, "engine");
    }
    recompute();
  });
  setups.addEventListener("change", (e) => {
    if (e.target.dataset.preset) applyPreset(e.target.closest(".setup"), e.target);
    else if (e.target.dataset.field === "shift_rpm") onShiftRpmCommit(e.target.closest(".setup"));
  });
  setups.addEventListener("click", (e) => {
    const root = e.target.closest(".setup");
    if (!root) return;

    if (e.target.closest("[data-add-gear]") || e.target.closest("[data-remove-gear]")) {
      const ratios = gearInputs(root).map((el) => parseFloat(el.value) || 1);
      if (e.target.closest("[data-add-gear]")) {
        if (ratios.length >= MAX_GEARS) return;
        const last = ratios[ratios.length - 1] ?? 1;
        buildGearRows(root, [...ratios, Math.max(0.1, +(last * 0.8).toFixed(2))]);
      } else {
        if (ratios.length <= MIN_GEARS) return;
        buildGearRows(root, ratios.slice(0, -1));
      }
      markCustom(root, "gearbox");
    } else if (e.target.closest("[data-add-point]") || e.target.closest("[data-remove-point]")) {
      const points = readCurve(root);
      if (e.target.closest("[data-add-point]")) {
        if (points.length >= MAX_CURVE_POINTS) return;
        // Extend the curve by one more even step, holding the last torque value.
        const [lastRpm, lastTorque] = points[points.length - 1] ?? [1000, 200];
        const step = points.length > 1 ? lastRpm - points[points.length - 2][0] : 1000;
        buildCurveRows(root, [...points, [lastRpm + Math.max(100, step), lastTorque]]);
      } else {
        if (points.length <= MIN_CURVE_POINTS) return;
        buildCurveRows(root, points.slice(0, -1));
      }
      markCustom(root, "engine");
    } else {
      return;
    }
    recompute();
  });

  $("#setup-tabs").addEventListener("click", (e) => {
    const tab = e.target.closest("[role=tab]");
    if (tab) selectTab(tab.dataset.setup);
  });

  $("#compare").addEventListener("change", onCompareChange);
  document.querySelectorAll('input[name="units"]').forEach((el) =>
    el.addEventListener("change", onUnitChange)
  );

  // Only moves the needles/markers along the traces; the curves are unchanged.
  $("#cur_speed").addEventListener("input", redraw);
}

async function main() {
  // The runtime's location is itself a fetch, so it cannot be started in
  // parallel with loading the runtime — but the two payloads can.
  const [config, src, presetData] = await Promise.all([
    fetch("config.json").then((r) => r.json()),
    fetch("calc.py").then((r) => r.text()),
    fetch("presets.json").then((r) => r.json()),
  ]);
  presets = presetData;

  const base = pyodideBaseUrl(config.pyodide);
  await loadScript(`${base}pyodide.js`);
  pyodide = await loadPyodide({ indexURL: base });
  pyodide.runPython(src);
  pyCompute = pyodide.globals.get("compute");
  pyAtSpeed = pyodide.globals.get("at_speed");
  pyTireDiameter = pyodide.globals.get("tire_diameter");
  pyPowerAtRpm = pyodide.globals.get("power_at_rpm");
  pyConvertCurve = pyodide.globals.get("convert_curve");

  // A fresh setup is the first preset of each, so `presets.json` holds the
  // defaults rather than a second copy of them living here.
  for (const key of SETUP_KEYS) {
    buildSetupForm(key);
    const root = setupRoot(key);
    buildGearRows(root, presets.gearboxes[0].gears);
    buildCurveRows(root, presets.engines[0].torque_curve);
    field(root, "max_rpm").value = String(presets.engines[0].redline);
    field(root, "shift_rpm").value = String(presets.engines[0].redline);
    presetSelect(root, "gearbox").value = "0";
    presetSelect(root, "engine").value = "0";
  }
  wireEvents();
  recompute();

  $("#loading").hidden = true;
  $("#app").hidden = false;
}

main().catch((err) => {
  console.error(err);
  const hint = runtimeHint ? `<br><small>${runtimeHint}</small>` : "";
  $("#loading").innerHTML =
    `<p role="alert">Failed to start Python.<br><small>${err}</small>${hint}</p>`;
});
