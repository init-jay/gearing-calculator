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
let pyConvertWeight = null;
let pyParseRacechrono = null;
let pyGearsAtSpeeds = null;

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

// Speedo majors are always a whole multiple of 20 (km/h or mph), like a real
// cluster. The spacing is the smallest such that the marker count — the majors
// plus the 0 — stays under SPEEDO_MAX_MARKERS, so a near-1:1 final drive that
// sends the top speed sky-high widens the scale instead of crowding the dial
// (20 for normal tops, 40 past ~280, 60/80/… beyond). mph tops out low enough
// that it never leaves 20.
const SPEEDO_MAX_MARKERS = 15;
function speedoScale(maxSpeed) {
  const maxMajors = SPEEDO_MAX_MARKERS - 1; // one marker is the 0
  const step = Math.max(20, Math.ceil(maxSpeed / maxMajors / 20) * 20);
  return { max: Math.ceil(maxSpeed / step) * step, step };
}

// One minor tick at each half-step, as on the reference cluster. Retune here.
const MINOR_PER_MAJOR = 2;

// Pulls the redline arc's ends off the two major ticks bounding it, so the red
// sits between them rather than running underneath.
const REDLINE_INSET_DEG = 2.5;

const R_BEZEL = 97;
const R_FACE = 95;
// Where the dials are cut flat. A bezel half-stroke (1.25) above the viewBox
// bottom (y=180), so the full 2.5-wide bezel border shows along the flat edge
// and that edge lands exactly on the element's bottom. Below every tick (y≈162)
// and label, so the flat only removes the empty dial arc.
const GAUGE_FLAT_Y = 178.75;
const R_TICK = 91.5; // outer edge of the tick ring; a small gap to the rim (R_FACE 95), not flush
const R_MAJOR_IN = 76;
const R_MINOR_IN = 81;
const R_RED = R_TICK - 4; // centreline of the redline arc (width 8), so its outer edge caps the tick ring
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
function renderGauge(svg, { needles, max, step, legend, redlineAt = null, thousands = max >= 1000 }) {
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

  // Flat-bottomed disk: the arc over the top closed by a straight chord along
  // the bottom, so the bezel border (and the dark face-edge just inside it) run
  // across the flat too — it reads as an intentional flat-bottom instrument,
  // not a cropped circle. The chord sits a bezel half-stroke above the viewBox
  // bottom (y=180) so the whole border shows and the flat edge lands on the
  // element's bottom, which the gear box aligns to.
  const flatDisk = (r) => {
    const dx = Math.sqrt(r * r - (GAUGE_FLAT_Y - cy) ** 2);
    return `M ${cx - dx} ${GAUGE_FLAT_Y} A ${r} ${r} 0 1 1 ${cx + dx} ${GAUGE_FLAT_Y} Z`;
  };
  svg.append(svgEl("path", { d: flatDisk(R_FACE), fill: `url(#${faceId})` }));
  svg.append(svgEl("path", { class: "gauge-bezel", d: flatDisk(R_BEZEL) }));

  // Numerals in thousands (with a "×1000" legend) keep a five-figure rpm dial
  // uncluttered. It defaults on past 1000 but is a per-dial choice: the speedo
  // opts out, so an absurdly tall final drive reading 1040 km/h shows "1040",
  // not "1.0", even though it crosses the same threshold.
  const big = thousands;
  const majors = Math.round(max / step);

  const labels = Array.from({ length: majors + 1 }, (_, i) => {
    const shown = big ? (i * step) / 1000 : i * step;
    return Number.isInteger(shown) ? String(shown) : shown.toFixed(1);
  });

  // One dial shows "7", another "300". Size the numerals to the widest of them
  // so three-digit scales don't collide and single-digit ones aren't tiny.
  const widest = Math.max(...labels.map((s) => s.length));
  svg.dataset.numWidth = widest <= 1 ? "narrow" : widest <= 2 ? "medium" : "wide";
  // A 20-major speedo packs many labels near the flat top of the arc; shrink
  // them so three-digit numbers there stop colliding. The tach (~7 majors) and
  // a coarser speedo stay at their full size.
  svg.dataset.dense = majors >= 10 ? "yes" : "no";

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

  const { xPix, yPix, top } = drawFrame(svg, {
    W, H, M, xMax, yMax,
    xTitle: `Speed (${series[0].result.speed_unit})`,
    yTitle: `Tractive effort (${series[0].result.force_unit})`,
  });

  // Drawn before the curves so they read on top of it. A limit above the tallest
  // curve is left undrawn rather than stretching the axis to reach it: nothing on
  // this chart is traction-limited, and the band would be an empty strip of sky.
  series.forEach((s) => {
    const limit = s.result.traction_limit;
    if (!(limit > 0) || limit >= yMax) return;
    const y = yPix(limit);
    svg.append(svgEl("rect", {
      class: `chart-grip-band chart-grip-band-${s.key}`,
      x: M.left, y: top, width: W - M.left - M.right, height: y - top,
    }));
    svg.append(svgEl("line", {
      class: `chart-grip-line chart-grip-line-${s.key}`,
      x1: M.left, y1: y, x2: W - M.right, y2: y,
    }));
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
  // Only when a band was actually drawn, on the same terms renderEffortChart uses.
  const yMax = Math.max(...series.map((s) => s.result.max_force)) * 1.05;
  if (series.some((s) => s.result.traction_limit > 0 && s.result.traction_limit < yMax)) {
    legendEntry(el, "swatch swatch-grip", "Beyond tire grip");
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

/** 1 -> "1st". Gears run 1..8, so the 11th/12th/13th exceptions cannot arise. */
const ordinal = (n) => `${n}${["th", "st", "nd", "rd"][n] ?? "th"}`;

/**
 * The standing-start time, plus the two things that most shape it: how much of
 * the run the tires rather than the engine limited, and how much of it was spent
 * shifting rather than accelerating.
 */
function renderAccel(el, series) {
  el.replaceChildren();
  const compare = series.length > 1;
  series.forEach((s) => {
    const a = s.result.acceleration;
    if (!a) return;
    const who = compare ? `Setup ${s.label}: ` : "";
    const unit = s.result.speed_unit;
    const run = `0–${a.target_speed} ${unit}`;

    const span = document.createElement("span");
    // Python's None arrives as `undefined`, not null, so test the number itself.
    if (!Number.isFinite(a.time)) {
      // Gearing that runs out of revs before the benchmark. Say so; a blank cell
      // reads as a bug, and a huge number reads as a slow car rather than a wall.
      span.textContent = `${who}never reaches ${a.target_speed} ${unit}`;
    } else {
      const parts = [`${run} ${a.time.toFixed(2)} s`];
      if (a.shifts > 0) {
        parts.push(`${a.shifts} shift${a.shifts > 1 ? "s" : ""} costing ` +
                   `${(a.shifts * s.inputs.shift_time).toFixed(2)} s`);
      }
      parts.push(a.traction_limited_to > 0
        ? `traction-limited below ${a.traction_limited_to.toFixed(0)} ${unit}` +
          ` in ${ordinal(a.traction_limited_gear)} gear`
        : "never traction-limited");
      span.textContent = who + parts.join(" · ");
    }
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
  for (const [kind, group] of Object.entries(PRESET_GROUPS)) {
    fillPresetSelect(presetSelect(root, kind), presets[group]);
  }
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
    weight: num(root, "weight", 1400),
    mu: num(root, "mu", 1.0),
    shift_time: num(root, "shift_time", 0.3),
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

/** Restate a weight in the other unit system. Scalar in, scalar out: no proxy to free. */
function convertWeight(value, units) {
  return pyConvertWeight(value, units);
}

/* -------------------------------- presets -------------------------------- */

const presetSelect = (root, kind) => root.querySelector(`[data-preset="${kind}"]`);

/** `data-preset` kind -> its array in presets.json. */
const PRESET_GROUPS = {
  gearbox: "gearboxes",
  engine: "engines",
  vehicle: "vehicles",
  tire: "tires",
};

const isPositive = (v) => Number.isFinite(v) && v > 0;
const isName = (v) => typeof v === "string" && v.length > 0;

/** The keys each preset group must supply, and what counts as a usable value. */
const PRESET_SHAPE = {
  gearboxes: {
    name: isName,
    gears: (v) => Array.isArray(v) && v.length > 0 && v.every(isPositive),
  },
  engines: {
    name: isName,
    redline: isPositive,
    torque_curve: (v) =>
      Array.isArray(v) && v.length >= 2 &&
      v.every((p) => Array.isArray(p) && p.length === 2 && p.every(isPositive)),
  },
  vehicles: { name: isName, weight: isPositive },
  tires: { name: isName, mu: isPositive },
};

/**
 * presets.json is meant to be hand-edited, so a missing or misspelled key is a
 * user error rather than an impossible state — and it has to be caught here, at
 * boot, where the message can still name the offending entry.
 *
 * Left unchecked it does not throw anywhere. `applyPreset` writes the missing
 * value into a number input as the string "undefined", which the input rejects
 * by going blank; `num()` then reads the blank as NaN and substitutes its
 * fallback. The result is a silently empty box beside a chart drawn from default
 * numbers, which reads as a rendering bug and is anything but.
 */
function validatePresets(data) {
  for (const [group, shape] of Object.entries(PRESET_SHAPE)) {
    const items = data?.[group];
    if (!Array.isArray(items) || items.length === 0) {
      throw new Error(`presets.json: "${group}" must be a non-empty array`);
    }
    items.forEach((item, i) => {
      for (const [key, isValid] of Object.entries(shape)) {
        if (!isValid(item?.[key])) {
          const who = isName(item?.name) ? `"${item.name}"` : `entry ${i}`;
          throw new Error(`presets.json: ${group} ${who} has a missing or invalid "${key}"`);
        }
      }
    });
  }
}

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
  const kind = sel.dataset.preset;
  const chosen = presets[PRESET_GROUPS[kind]][sel.value];
  if (!chosen) return; // re-picking "Custom" keeps whatever is on screen

  if (kind === "gearbox") {
    buildGearRows(root, chosen.gears);
  } else if (kind === "engine") {
    // The redline belongs to the engine, so it comes along. Presets are metric.
    const curve = currentUnits() === "metric"
      ? chosen.torque_curve
      : convertCurve(chosen.torque_curve, "imperial");
    buildCurveRows(root, curve);
    field(root, "max_rpm").value = String(chosen.redline);
    // Keep an earlier shift point if the user chose one; never exceed the redline.
    const shift = num(root, "shift_rpm", chosen.redline);
    field(root, "shift_rpm").value = String(Math.min(shift, chosen.redline));
  } else if (kind === "vehicle") {
    // Weights are stored in kg, like torque curves are stored in N·m.
    const metric = currentUnits() === "metric";
    const weight = metric ? chosen.weight : convertWeight(chosen.weight, "imperial");
    field(root, "weight").value = metric ? String(weight) : weight.toFixed(0);
  } else {
    // A coefficient of friction is a pure ratio, so no unit conversion.
    field(root, "mu").value = String(chosen.mu);
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
  for (const kind of Object.keys(PRESET_GROUPS)) {
    presetSelect(dst, kind).value = presetSelect(src, kind).value;
  }
}

/* ------------------------------ inputs drawer ---------------------------- */

/* Matches the CSS breakpoint where the drawer becomes an inline column. The
 * hamburger serves both modes: overlay drawer on phones, column collapse on
 * desktop — two orthogonal states, each ignored by the other mode's CSS. */
const desktopLayout = matchMedia("(min-width: 56.25rem)");

/** Open/close the off-canvas inputs panel (phone mode). Results keep updating
 * live behind the backdrop, so tweaking a ratio shows its effect without
 * closing. */
function setInputsDrawer(open) {
  $("#inputs-drawer").classList.toggle("open", open);
  $("#drawer-backdrop").hidden = !open;
  $("#inputs-toggle").setAttribute("aria-expanded", String(open));
  if (open) {
    $("#inputs-close").focus();
  } else {
    $("#inputs-toggle").focus();
  }
}

function initInputsDrawer() {
  const toggle = $("#inputs-toggle");
  toggle.addEventListener("click", () => {
    if (desktopLayout.matches) {
      const collapsed = $(".layout").classList.toggle("inputs-collapsed");
      toggle.setAttribute("aria-expanded", String(!collapsed));
    } else {
      setInputsDrawer(true);
    }
  });
  $("#inputs-close").addEventListener("click", () => setInputsDrawer(false));
  $("#drawer-backdrop").addEventListener("click", () => setInputsDrawer(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !desktopLayout.matches &&
        $("#inputs-drawer").classList.contains("open")) {
      setInputsDrawer(false);
    }
  });

  // aria-expanded answers for whichever mode is live, on load and whenever a
  // resize crosses the breakpoint.
  const syncAria = () => toggle.setAttribute("aria-expanded", String(
    desktopLayout.matches
      ? !$(".layout").classList.contains("inputs-collapsed")
      : $("#inputs-drawer").classList.contains("open")
  ));
  desktopLayout.addEventListener("change", syncAria);
  syncAria();
}

/* -------------------------------- lap map -------------------------------- */
/*
 * RaceChrono CSV -> speed-shaded GPS trace. Fully independent of the gearing
 * setups: its own upload, its own state, never part of recompute()/redraw().
 * Parsing lives in lapmap.py (loaded into Pyodide beside calc.py); this side
 * only fits the projected meters into the viewBox and colors the segments.
 */

const LAP_W = 640;
const LAP_H = 480;
const LAP_MARGIN = 24;

/**
 * Sequential single-hue ramp (blue, steps 100-700). One hue light->dark, so
 * magnitude reads as ink density rather than a rainbow. The near-minimum end
 * recedes toward the chart surface: lightest step in light mode, darkest in
 * dark mode — hence the reversal.
 */
const SPEED_RAMP = [
  "#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5",
  "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b",
];

/**
 * Categorical palette for the gear-shaded maps, indexed by gear - 1. Identity,
 * not magnitude: "3rd" is a thing you are in, not a level of something, and
 * adjacent gears must be tellable apart at a glance — a ramp would smear them.
 * Fixed slot order (the order *is* the colorblind-safety mechanism); the dark
 * column is the same hues re-stepped for the dark surface, not a new palette.
 * Eight slots cover MAX_GEARS exactly.
 */
const GEAR_PALETTE = {
  light: ["#2a78d6", "#1baf7a", "#eda100", "#008300", "#4a3aa7", "#e34948", "#e87ba4", "#eb6834"],
  dark: ["#3987e5", "#199e70", "#c98500", "#008300", "#9085e9", "#e66767", "#d55181", "#d95926"],
};

const darkMode = matchMedia("(prefers-color-scheme: dark)");

/** Color for a normalized speed t in [0, 1] under the current color scheme. */
function speedColor(t) {
  const ramp = darkMode.matches ? [...SPEED_RAMP].reverse() : SPEED_RAMP;
  const i = Math.min(ramp.length - 1, Math.max(0, Math.round(t * (ramp.length - 1))));
  return ramp[i];
}

/** Color for a 1-based gear under the current color scheme. */
function gearColor(gear) {
  const slots = GEAR_PALETTE[darkMode.matches ? "dark" : "light"];
  return slots[Math.min(Math.max(gear, 1), slots.length) - 1];
}

/** m/s -> the display unit selected for the rest of the app. */
const speedInUnits = (mps) =>
  currentUnits() === "metric" ? mps * 3.6 : mps * 2.2369363;
const lapSpeedUnit = () => (currentUnits() === "metric" ? "km/h" : "mph");

/** Seconds -> "1:31.29". Laps are minutes long, so hours never arise. */
function formatLapTime(s) {
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(2).padStart(5, "0")}`;
}

/** Upload result plus which lap is on screen and what shades the trace
 * ("speed" | "gear"). Replaced whole on re-upload, except the color mode,
 * which is a viewing preference rather than a property of the file. */
let lapState = { data: null, lapIndex: 0, colorMode: "speed" };

function initLapMap() {
  $("#lap-file").addEventListener("change", onLapFile);
  $("#lap-select").addEventListener("change", (e) => {
    lapState.lapIndex = parseInt(e.target.value, 10);
    renderLapSection();
  });
  document.querySelectorAll('input[name="lap-color"]').forEach((el) =>
    el.addEventListener("change", () => {
      lapState.colorMode = el.value;
      renderLapSection();
    })
  );
  // The cornering threshold lives in the strategy blurb, which only shows in
  // gear mode — the only mode the value affects.
  $("#lap-corner-g").addEventListener("input", renderLapSection);
  // The colors are baked into stroke attributes, not CSS variables, so a
  // scheme flip has to redraw rather than restyle.
  darkMode.addEventListener("change", () => {
    if (lapState.data) renderLapSection();
  });
}

async function onLapFile(e) {
  const file = e.target.files[0];
  if (!file) return;
  const errEl = $("#lap-error");
  const { colorMode } = lapState;
  try {
    const text = await file.text();
    lapState = { data: callPyArgs(pyParseRacechrono, text), lapIndex: 0, colorMode };
  } catch (err) {
    lapState = { data: null, lapIndex: 0, colorMode };
    // Pyodide wraps the ValueError in a traceback; the message is on its
    // last non-empty line, reading e.g. "ValueError: Missing column(s): ...".
    const lines = String(err.message ?? err).trim().split("\n");
    errEl.textContent = lines[lines.length - 1].replace(/^ValueError:\s*/, "");
    errEl.hidden = false;
    $("#lap-view").hidden = true;
    $("#lap-select-label").hidden = true;
    $("#lap-color-by").hidden = true;
    return;
  }
  errEl.hidden = true;
  lapState.lapIndex = defaultLapIndex(lapState.data.laps);
  buildLapSelect(lapState.data.laps);
  $("#lap-color-by").hidden = false;
  renderLapSection();
}

/** Fastest complete lap; a file with no complete laps falls back to the one
 * with the most GPS points (the most track drawn). */
function defaultLapIndex(laps) {
  let best = -1;
  laps.forEach((lap, i) => {
    if (lap.complete && (best < 0 || lap.duration < laps[best].duration)) best = i;
  });
  if (best >= 0) return best;
  laps.forEach((lap, i) => {
    if (best < 0 || lap.x.length > laps[best].x.length) best = i;
  });
  return best;
}

function buildLapSelect(laps) {
  const select = $("#lap-select");
  select.replaceChildren();
  laps.forEach((lap, i) => {
    const label = `Lap ${lap.lap} — ${formatLapTime(lap.duration)}` +
                  (lap.complete ? "" : " (partial)");
    select.append(new Option(label, String(i), false, i === lapState.lapIndex));
  });
  $("#lap-select-label").hidden = laps.length < 2;
}

function renderLapSection() {
  const lap = lapState.data?.laps[lapState.lapIndex];
  if (!lap) return;
  // Build the specs before touching the DOM: gear mode reads the live setup
  // forms, and a mid-edit form (or a Python error) must keep the last good
  // render on screen rather than blanking the maps.
  const specs = lapState.colorMode === "gear" ? lapGearSpecs(lap) : [lapSpeedSpec(lap)];
  if (!specs) return;

  $("#lap-view").hidden = false;
  const maps = $("#lap-maps");
  maps.replaceChildren();
  for (const spec of specs) {
    const figure = document.createElement("figure");
    figure.className = "lap-figure";
    const svg = svgEl("svg", {
      class: "chart lap-map",
      viewBox: `0 0 ${LAP_W} ${LAP_H}`,
      role: "img",
      "aria-label": spec.ariaLabel,
    });
    figure.append(svg);
    if (spec.caption) {
      const cap = document.createElement("figcaption");
      cap.className = `lap-cap lap-cap-${spec.key}`;
      cap.textContent = spec.caption;
      figure.append(cap);
    }
    maps.append(figure);
    renderLapTrace(svg, lap, spec);
  }
  renderLapLegend($("#lap-legend"), lap, specs);
  $("#lap-strategy").hidden = lapState.colorMode !== "gear";
}

/** The one-map spec of the original feature: shade by GPS speed. */
function lapSpeedSpec(lap) {
  const range = lap.speed_max - lap.speed_min;
  return {
    key: "speed",
    caption: null,
    ariaLabel: "Track map traced from GPS, colored by speed",
    segColor: (i) => {
      const v = (lap.speed[i - 1] + lap.speed[i]) / 2;
      return speedColor(range > 0 ? (v - lap.speed_min) / range : 0.5);
    },
    readout: (i) =>
      `${speedInUnits(lap.speed[i]).toFixed(0)} ${lapSpeedUnit()} · ` +
      formatLapTime(lap.t[i]),
  };
}

/**
 * One spec per active setup, shaded by the gear that setup's shift schedule
 * holds at each sample's speed — with comparison on, the same lap twice, so
 * the two gearsets can be read corner by corner. Returns null (keep the last
 * render) when a setup is mid-edit and has no valid gears yet.
 */
/** Cornering threshold (G) from the blurb's input; mid-edit blanks fall back
 * to the model's default rather than disabling the corner hold. */
function lapCornerG() {
  const v = parseFloat($("#lap-corner-g").value);
  return Number.isFinite(v) && v >= 0 ? v : 0.4;
}

function lapGearSpecs(lap) {
  const speeds = lap.speed.map(speedInUnits);
  const cornerG = lapCornerG();
  const compare = comparing();
  const specs = [];
  for (const key of activeKeys()) {
    const inputs = readInputs(setupRoot(key));
    if (inputs.gears.length === 0) return null;
    let gears;
    try {
      // The JS arrays cross as iterable proxies, which gears_at_speeds
      // materializes. Times and lateral G let it charge shift dead time and
      // hold gears through loaded corners.
      gears = callPy(
        pyGearsAtSpeeds, inputs, speeds, lap.t, lap.lat_g ?? null, cornerG
      );
    } catch (err) {
      console.error(err);
      return null;
    }
    const label = compare ? `Setup ${key.toUpperCase()}` : null;
    specs.push({
      key,
      caption: label,
      ariaLabel: `Track map colored by gear${label ? `, ${label}` : ""}`,
      gears,
      segColor: (i) => gearColor(gears[i]),
      readout: (i) =>
        `${ordinal(gears[i])} gear · ` +
        `${speedInUnits(lap.speed[i]).toFixed(0)} ${lapSpeedUnit()} · ` +
        formatLapTime(lap.t[i]),
    });
  }
  return specs;
}

function renderLapTrace(svg, lap, spec) {
  svg.replaceChildren();

  // Fit the lap's meters into the viewBox, uniform scale, centered. SVG y
  // grows downward while the projection's grows northward, so y flips.
  const xMin = Math.min(...lap.x), xMax = Math.max(...lap.x);
  const yMin = Math.min(...lap.y), yMax = Math.max(...lap.y);
  const span = Math.max(xMax - xMin, yMax - yMin, 1e-9);
  const scale = Math.min(LAP_W - 2 * LAP_MARGIN, LAP_H - 2 * LAP_MARGIN) / span;
  const xOff = (LAP_W - (xMax - xMin) * scale) / 2;
  const yOff = (LAP_H - (yMax - yMin) * scale) / 2;
  const px = (x) => xOff + (x - xMin) * scale;
  const py = (y) => LAP_H - yOff - (y - yMin) * scale;

  // SVG has no per-vertex gradient on a polyline, so the shading is one short
  // <line> per sample pair; round caps make the joins seamless.
  const g = svgEl("g", { class: "lap-trace" });
  for (let i = 1; i < lap.x.length; i++) {
    g.append(svgEl("line", {
      x1: px(lap.x[i - 1]), y1: py(lap.y[i - 1]),
      x2: px(lap.x[i]), y2: py(lap.y[i]),
      stroke: spec.segColor(i),
      "stroke-width": 5,
      "stroke-linecap": "round",
    }));
  }
  svg.append(g);
  svg.append(svgEl("circle", {
    class: "lap-start", cx: px(lap.x[0]), cy: py(lap.y[0]), r: 6,
  }));

  // Hover: nearest sample by squared distance (a linear scan over <= ~1200
  // points), marked on the trace with the speed/time read out at the top.
  const marker = svgEl("circle", { class: "lap-hover-marker", r: 7 });
  const readout = svgEl("text", {
    class: "lap-readout", x: LAP_MARGIN, y: LAP_MARGIN - 6,
  });
  marker.style.display = "none";
  svg.append(marker, readout);

  svg.onpointermove = (e) => {
    const rect = svg.getBoundingClientRect();
    const mx = ((e.clientX - rect.left) / rect.width) * LAP_W;
    const my = ((e.clientY - rect.top) / rect.height) * LAP_H;
    let best = 0, bestD = Infinity;
    for (let i = 0; i < lap.x.length; i++) {
      const d = (px(lap.x[i]) - mx) ** 2 + (py(lap.y[i]) - my) ** 2;
      if (d < bestD) { bestD = d; best = i; }
    }
    marker.setAttribute("cx", px(lap.x[best]));
    marker.setAttribute("cy", py(lap.y[best]));
    marker.style.display = "";
    readout.textContent = spec.readout(best);
  };
  svg.onpointerleave = () => {
    marker.style.display = "none";
    readout.textContent = "";
  };
}

function renderLapLegend(el, lap, specs) {
  el.replaceChildren();

  const title = document.createElement("span");
  const where = lapState.data.track || lapState.data.session;
  title.textContent = `${where ? where + " — " : ""}Lap ${lap.lap}, ` +
                      `${formatLapTime(lap.duration)}` +
                      (lap.complete ? "" : " (partial)");
  el.append(title);

  if (lapState.colorMode === "gear") {
    // One chip per gear this lap actually reaches, across every rendered
    // setup — the same color means the same gear on both maps, so the legend
    // is shared rather than repeated under each.
    const used = [...new Set(specs.flatMap((s) => s.gears))].sort((a, b) => a - b);
    const row = document.createElement("div");
    row.className = "lap-gear-chips";
    for (const gear of used) {
      const chip = document.createElement("span");
      const sw = document.createElement("i");
      sw.className = "swatch";
      sw.style.background = gearColor(gear);
      chip.append(sw, document.createTextNode(ordinal(gear)));
      row.append(chip);
    }
    el.append(row);
    return;
  }

  const scale = document.createElement("div");
  scale.className = "lap-scale-row";
  const lo = document.createElement("span");
  lo.textContent = `${speedInUnits(lap.speed_min).toFixed(0)} ${lapSpeedUnit()}`;
  const hi = document.createElement("span");
  hi.textContent = `${speedInUnits(lap.speed_max).toFixed(0)} ${lapSpeedUnit()}`;
  const bar = document.createElement("div");
  bar.className = "lap-scale";
  const ramp = darkMode.matches ? [...SPEED_RAMP].reverse() : SPEED_RAMP;
  bar.style.background = `linear-gradient(to right, ${ramp.join(", ")})`;
  scale.append(lo, bar, hi);
  el.append(scale);
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

/** Call a Python function with positional scalar/string args (which Pyodide
 * converts implicitly, unlike the object `callPy` wraps with `toPy`). */
function callPyArgs(fn, ...args) {
  let proxy;
  try {
    proxy = fn(...args);
    return proxy.toJs({ dict_converter: Object.fromEntries });
  } finally {
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

  // Gear-shaded lap maps read the setups' shift schedules, so they follow
  // gearing edits (and comparison toggles) the same way the charts do.
  if (lapState.data && lapState.colorMode === "gear") renderLapSection();
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
  const maxSpeed = Math.max(1, ...series.map((s) => s.result.max_speed));
  renderGauge($("#speedo"), {
    needles: [{ value: speed, cls: "gauge-needle" }],
    ...speedoScale(maxSpeed),
    legend: [series[0].result.speed_unit],
    thousands: false, // a speedo reads whole km/h, never "×1000"
  });

  // The gear each setup holds at this speed, in the cluster between the dials.
  // RPM is no longer shown digitally — it is read off the tach face.
  for (const s of series) {
    const out = s.key === "a" ? $("#cur_gear") : $(`#cur_gear_${s.key}`);
    out.textContent = String(s.at.gear);
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
    renderAccel($("#accel"), series);
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
  $(".gauges").toggleAttribute("data-compare", on);
  // B's gear and the "/" separator show only while comparing; A is always on.
  $("#cur_gear_b").hidden = !on;
  $(".gear-sep").hidden = !on;

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
  document.querySelectorAll("[data-weight-unit]").forEach((el) => {
    el.textContent = metric ? "kg" : "lb";
  });

  // Torque, unlike the tire size, has no unit-agnostic source to re-derive from:
  // the numbers on screen *are* the data, so they have to be restated in place.
  // Both setups convert, even the inactive one, or B would silently keep lb-ft.
  // Weight is the same story; mu is a pure ratio and stays as it is.
  for (const key of SETUP_KEYS) {
    const root = setupRoot(key);
    buildCurveRows(root, convertCurve(readCurve(root), currentUnits()));
    field(root, "weight").value = convertWeight(num(root, "weight", 1400), currentUnits()).toFixed(0);
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

  // The lap map legend/readout quote speeds in the selected unit too.
  if (lapState.data) renderLapSection();
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
      else if (e.target.dataset.field === "weight") markCustom(root, "vehicle");
      else if (e.target.dataset.field === "mu") markCustom(root, "tire");
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

  initLapMap();
  initInputsDrawer();
}

async function main() {
  // The runtime's location is itself a fetch, so it cannot be started in
  // parallel with loading the runtime — but the two payloads can.
  const [config, src, presetData, lapmapSrc] = await Promise.all([
    fetch("config.json").then((r) => r.json()),
    fetch("calc.py").then((r) => r.text()),
    fetch("presets.json").then((r) => r.json()),
    fetch("lapmap.py").then((r) => r.text()),
  ]);
  validatePresets(presetData);
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
  pyConvertWeight = pyodide.globals.get("convert_weight");
  pyodide.runPython(lapmapSrc);
  pyParseRacechrono = pyodide.globals.get("parse_racechrono");
  pyGearsAtSpeeds = pyodide.globals.get("gears_at_speeds");

  // A fresh setup is the first preset of each, so `presets.json` holds the
  // defaults rather than a second copy of them living here.
  for (const key of SETUP_KEYS) {
    buildSetupForm(key);
    const root = setupRoot(key);
    buildGearRows(root, presets.gearboxes[0].gears);
    buildCurveRows(root, presets.engines[0].torque_curve);
    field(root, "max_rpm").value = String(presets.engines[0].redline);
    field(root, "shift_rpm").value = String(presets.engines[0].redline);
    field(root, "weight").value = String(presets.vehicles[0].weight);
    field(root, "mu").value = String(presets.tires[0].mu);
    presetSelect(root, "gearbox").value = "0";
    presetSelect(root, "engine").value = "0";
    presetSelect(root, "vehicle").value = "0";
    presetSelect(root, "tire").value = "0";
  }
  wireEvents();
  // Browsers restore a checked #compare across a reload, but the `change`
  // event that builds the comparison UI does not fire on restore. Run the
  // handler once so the tabs, dual gauges and B setup match the checkbox
  // whatever state it loaded in; it calls `recompute` itself. (Unchecked —
  // the usual case — this is a harmless single-setup sync.)
  onCompareChange();

  $("#loading").hidden = true;
  $("#app").hidden = false;
}

main().catch((err) => {
  console.error(err);
  // Reached by a bad presets.json as well as by a runtime that will not load, so
  // the headline stays neutral; `runtimeHint` is only set once the former passed.
  const hint = runtimeHint ? `<br><small>${runtimeHint}</small>` : "";
  $("#loading").innerHTML =
    `<p role="alert">Failed to start.<br><small>${err}</small>${hint}</p>`;
});
