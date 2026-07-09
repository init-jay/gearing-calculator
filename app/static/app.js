/*
 * Gearing calculator frontend.
 *
 * All drivetrain math lives in calc.py and runs in the browser under Pyodide;
 * this file only gathers inputs, calls into Python, and draws SVG.
 */

const DEFAULT_GEARS = [5.14, 2.83, 1.79, 1.26, 1.0, 0.83];
const MAX_GEARS = 8;
const MIN_GEARS = 1;

const $ = (sel) => document.querySelector(sel);

let pyodide = null;
let pyCompute = null;
let pyAtSpeed = null;
let pyTireDiameter = null;
let lastResult = null;

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

/**
 * Draw a round instrument dial: dark face, tick ring, numerals, tapered needle,
 * and `legend` printed below the hub. Like the real cluster it carries no digital
 * readout; the live rpm is shown between the dials instead.
 * `redlineAt` (a value, optional) marks the band from there to `max` in red.
 */
function renderGauge(svg, { value, max, step, legend, redlineAt = null }) {
  const cx = 100;
  const cy = 100;
  const clamped = Math.max(0, Math.min(value, max));
  const angleFor = (v) => GAUGE_START + (max > 0 ? v / max : 0) * GAUGE_SWEEP;
  const valueAngle = angleFor(clamped);
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
      // Minor ticks would collide with the red bars, so the band gets only bars.
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

  // Tapered needle: narrow at the tip, wide across the tail. `u` runs along the
  // needle, `p` across it.
  const rad = ((valueAngle - 90) * Math.PI) / 180;
  const [ux, uy] = [Math.cos(rad), Math.sin(rad)];
  const [px, py] = [-uy, ux];
  const pt = (along, across) => `${cx + along * ux + across * px},${cy + along * uy + across * py}`;
  svg.append(
    svgEl("polygon", {
      class: "gauge-needle",
      points: [
        pt(R_NEEDLE, 1.0),
        pt(R_NEEDLE, -1.0),
        pt(-NEEDLE_TAIL, -3.6),
        pt(-NEEDLE_TAIL, 3.6),
      ].join(" "),
    })
  );

  // Drawn after the needle so its base disappears beneath the hub cap.
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

function renderChart(svg, result, current) {
  const W = 640;
  const H = 360;
  // Every gear curve tops out at max RPM, so the top margin has to clear the
  // gear numbers printed above where each one ends.
  const M = { top: 24, right: 16, bottom: 44, left: 52 };
  const plotW = W - M.left - M.right;
  const plotH = H - M.top - M.bottom;

  // Road speed runs along X because it increases monotonically through a run,
  // so the shift trace reads left-to-right as a sawtooth.
  const xMax = result.max_speed * 1.05 || 1;
  const yMax = result.max_rpm;

  const xPix = (spd) => M.left + (spd / xMax) * plotW;
  const yPix = (rpm) => M.top + plotH - (rpm / yMax) * plotH;

  svg.replaceChildren();

  // Grid + axis labels.
  const yStep = niceStep(yMax / 5);
  for (let v = 0; v <= yMax; v += yStep) {
    const y = yPix(v);
    svg.append(svgEl("line", { class: "chart-grid", x1: M.left, y1: y, x2: W - M.right, y2: y }));
    svg.append(
      svgEl("text", { class: "chart-label", x: M.left - 8, y: y + 4, "text-anchor": "end" },
        String(Math.round(v)))
    );
  }

  const xStep = niceStep(xMax / 6);
  for (let v = 0; v <= xMax; v += xStep) {
    const x = xPix(v);
    svg.append(svgEl("line", { class: "chart-grid", x1: x, y1: M.top, x2: x, y2: M.top + plotH }));
    svg.append(
      svgEl("text", { class: "chart-label", x, y: M.top + plotH + 18, "text-anchor": "middle" },
        String(Math.round(v)))
    );
  }

  // Axes.
  svg.append(svgEl("line", { class: "chart-axis", x1: M.left, y1: M.top, x2: M.left, y2: M.top + plotH }));
  svg.append(
    svgEl("line", { class: "chart-axis", x1: M.left, y1: M.top + plotH, x2: W - M.right, y2: M.top + plotH })
  );
  svg.append(
    svgEl("text", { class: "chart-axis-title", x: M.left + plotW / 2, y: H - 6, "text-anchor": "middle" },
      `Speed (${result.speed_unit})`)
  );
  svg.append(
    svgEl("text", {
      class: "chart-axis-title", x: 14, y: M.top + plotH / 2,
      "text-anchor": "middle", transform: `rotate(-90 14 ${M.top + plotH / 2})`,
    }, "Engine RPM")
  );

  // One polyline per gear. They share a color, so each is named where it ends —
  // at max RPM, spread along the top edge by the gear's top speed.
  result.curves.forEach((curve) => {
    const pts = curve.samples.map(([rpm, spd]) => `${xPix(spd)},${yPix(rpm)}`).join(" ");
    svg.append(svgEl("polyline", { class: "chart-line", points: pts }));
    svg.append(
      svgEl("text", {
        class: "chart-gear-label", x: xPix(curve.top_speed), y: M.top - 6, "text-anchor": "middle",
      }, String(curve.gear))
    );
  });

  // The acceleration run: up each gear to the shift RPM, then straight down to
  // the next gear at the same road speed. Drawn over the curves it annotates.
  if (result.trace.length) {
    const pts = result.trace.map(([rpm, spd]) => `${xPix(spd)},${yPix(rpm)}`).join(" ");
    svg.append(svgEl("polyline", { class: "chart-trace", points: pts }));
    result.shifts.forEach((s) => {
      svg.append(
        svgEl("circle", { class: "chart-shift", cx: xPix(s.speed), cy: yPix(result.shift_rpm), r: 3 })
      );
    });
  }

  // Marker for the current speed, in whichever gear the shift schedule puts it.
  if (current && current.speed >= 0 && current.speed <= xMax) {
    svg.append(
      svgEl("circle", { class: "chart-marker", cx: xPix(current.speed), cy: yPix(current.rpm), r: 4 })
    );
  }
}

/** The gear curves are named on the plot itself, so only the trace needs a key. */
function renderLegend(el, result) {
  el.replaceChildren();

  if (result.shifts.length) {
    const span = document.createElement("span");
    const sw = document.createElement("i");
    sw.className = "swatch swatch-trace";
    span.append(sw, document.createTextNode(`Shift trace (@ ${Math.round(result.shift_rpm)} rpm)`));
    el.append(span);
  }
}

/* --------------------------------- table --------------------------------- */

function renderTable(tbody, result, inputs, currentGear) {
  tbody.replaceChildren();
  result.curves.forEach((curve) => {
    const overall = curve.ratio * inputs.final_drive * inputs.transfer;
    const tr = document.createElement("tr");
    if (curve.gear === currentGear) tr.className = "is-current";
    for (const text of [
      String(curve.gear),
      curve.ratio.toFixed(3),
      overall.toFixed(3),
      curve.top_speed.toFixed(1),
    ]) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.append(td);
    }
    tbody.append(tr);
  });
}

/** One row per upshift: where the engine lands in the next gear. */
function renderShiftTable(tbody, result) {
  tbody.replaceChildren();
  result.shifts.forEach((s) => {
    const tr = document.createElement("tr");
    for (const text of [
      `${s.from_gear} → ${s.to_gear}`,
      s.speed.toFixed(1),
      s.rpm_after.toFixed(1),
      String(Math.round(s.rpm_drop)),
    ]) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.append(td);
    }
    tbody.append(tr);
  });
}

/* --------------------------------- inputs -------------------------------- */

function gearInputs() {
  return [...document.querySelectorAll(".gear-input")];
}

const currentUnits = () => document.querySelector('input[name="units"]:checked').value;

const num = (id, fallback) => {
  const v = parseFloat($(id).value);
  return Number.isFinite(v) ? v : fallback;
};

/**
 * Derive the tire diameter from the 225/45R17-style size inputs and write it
 * into the read-only `#tire` field, in whichever unit is currently selected.
 * Must run before `readInputs`, which treats `#tire` as the source of truth.
 */
function updateTireDiameter() {
  const units = currentUnits();
  const dia = pyTireDiameter(
    num("#tire_width", 225), num("#tire_aspect", 45), num("#tire_wheel", 17), units
  );
  $("#tire").value = dia.toFixed(units === "metric" ? 1 : 2);
}

function readInputs() {
  return {
    gears: gearInputs().map((el) => parseFloat(el.value)).filter((v) => Number.isFinite(v) && v > 0),
    final_drive: num("#final_drive", 3.64),
    transfer: num("#transfer", 1.0),
    tire: num("#tire", 634.3),
    slip: num("#slip", 0) / 100,
    max_rpm: num("#max_rpm", 7000),
    shift_rpm: num("#shift_rpm", 7000),
    units: currentUnits(),
  };
}

function buildGearRows(ratios) {
  const list = $("#gear-list");
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
  syncGearButtons();
}

function syncGearButtons() {
  const n = gearInputs().length;
  $("#add-gear").disabled = n >= MAX_GEARS;
  $("#remove-gear").disabled = n <= MIN_GEARS;
}

/* ------------------------------ python bridge ---------------------------- */

/** Recompute all gear curves via Python, then redraw everything. */
function recompute() {
  updateTireDiameter();
  const inputs = readInputs();
  if (inputs.gears.length === 0) return;

  // Advertise the ceiling, but don't rewrite the value here: `recompute` runs on
  // every keystroke, and a half-typed redline ("8" of "8000") would clobber it.
  // Python clamps the shift RPM to the redline anyway, so the math stays right;
  // `onShiftRpmCommit` tidies the field once the user is done typing.
  $("#shift_rpm").max = String(inputs.max_rpm);

  const pyIn = pyodide.toPy(inputs);
  let proxy;
  try {
    proxy = pyCompute(pyIn);
    lastResult = proxy.toJs({ dict_converter: Object.fromEntries });
  } finally {
    pyIn.destroy();
    if (proxy) proxy.destroy();
  }

  // The speed slider's ceiling is the top gear's top speed, so it can only be
  // set once `lastResult` is fresh. `floor` keeps the slider inside the plot.
  const slider = $("#cur_speed");
  slider.max = String(Math.floor(lastResult.max_speed));
  if (parseFloat(slider.value) > lastResult.max_speed) slider.value = slider.max;

  document.querySelectorAll("[data-speed-unit]").forEach((el) => {
    el.textContent = lastResult.speed_unit;
  });

  redraw();
}

/** Redraw gauges/chart/table from `lastResult` + the current road speed. */
function redraw() {
  if (!lastResult) return;
  const inputs = readInputs();
  if (inputs.gears.length === 0) return;

  const speed = parseFloat($("#cur_speed").value);
  $("#cur_speed_out").textContent = String(Math.round(speed));

  // Python decides which gear the shift schedule puts us in, and the rpm that
  // gear needs to hold this speed — so the marker always lands on the trace.
  const pyIn = pyodide.toPy(inputs);
  let proxy, at;
  try {
    proxy = pyAtSpeed(pyIn, speed);
    at = proxy.toJs({ dict_converter: Object.fromEntries });
  } finally {
    pyIn.destroy();
    if (proxy) proxy.destroy();
  }

  const gearNum = at.gear;
  const rpm = at.rpm;
  $("#cur_gear").textContent = String(gearNum);

  const tach = rpmScale(inputs.max_rpm);
  renderGauge($("#tach"), {
    value: rpm, ...tach,
    legend: tach.max >= 1000 ? ["1/min", "×1000"] : ["1/min"],
    redlineAt: tach.max - tach.step, // the last major segment, i.e. the top 1000 rpm
  });

  const speedScale = gaugeScale(Math.max(lastResult.max_speed, 1));
  renderGauge($("#speedo"), {
    value: speed, ...speedScale, legend: [lastResult.speed_unit],
  });

  $("#rpm_out").textContent = String(Math.round(rpm));

  renderChart($("#chart"), lastResult, { rpm, speed });
  renderLegend($("#legend"), lastResult);
  renderTable($("#table tbody"), lastResult, inputs, gearNum);
  renderShiftTable($("#shift-table tbody"), lastResult);
}

/* ---------------------------------- init --------------------------------- */

function onUnitChange() {
  const slider = $("#cur_speed");
  const oldMax = lastResult ? lastResult.max_speed : 0;
  const oldValue = parseFloat(slider.value);

  // The tire size itself is unit-agnostic; `recompute` re-derives the diameter
  // in the newly selected unit, so only the label needs updating here.
  document.querySelector("[data-tire-unit]").textContent =
    currentUnits() === "metric" ? "mm" : "in";
  recompute();

  // Keep the slider on the same physical speed. Both the old and new top speeds
  // scale by the same factor across a unit change, so their ratio converts the
  // value exactly — no hardcoded 1.609344. Scale from the pre-clamp value, since
  // `recompute` may have pulled it down to the new (smaller) ceiling.
  if (oldMax > 0 && Number.isFinite(oldValue)) {
    const scaled = Math.round(oldValue * (lastResult.max_speed / oldMax));
    slider.value = String(Math.min(scaled, parseFloat(slider.max)));
    redraw();
  }
}

/** Once the user commits a shift RPM, pull it back under the redline. */
function onShiftRpmCommit() {
  const el = $("#shift_rpm");
  const maxRpm = num("#max_rpm", 7000);
  if (num("#shift_rpm", maxRpm) > maxRpm) {
    el.value = String(maxRpm);
    recompute();
  }
}

function wireEvents() {
  // Any change to a calculator input re-runs the Python math.
  for (const id of [
    "#tire_width", "#tire_aspect", "#tire_wheel",
    "#final_drive", "#transfer", "#slip", "#max_rpm", "#shift_rpm",
  ]) {
    $(id).addEventListener("input", recompute);
  }
  $("#shift_rpm").addEventListener("change", onShiftRpmCommit);
  $("#gear-list").addEventListener("input", recompute);
  document.querySelectorAll('input[name="units"]').forEach((el) =>
    el.addEventListener("change", onUnitChange)
  );

  // Only moves the needles/marker along the trace; the curves are unchanged.
  $("#cur_speed").addEventListener("input", redraw);

  $("#add-gear").addEventListener("click", () => {
    const ratios = gearInputs().map((el) => parseFloat(el.value) || 1);
    if (ratios.length >= MAX_GEARS) return;
    const last = ratios[ratios.length - 1] ?? 1;
    buildGearRows([...ratios, Math.max(0.1, +(last * 0.8).toFixed(2))]);
    recompute();
  });

  $("#remove-gear").addEventListener("click", () => {
    const ratios = gearInputs().map((el) => parseFloat(el.value) || 1);
    if (ratios.length <= MIN_GEARS) return;
    buildGearRows(ratios.slice(0, -1));
    recompute();
  });
}

async function main() {
  pyodide = await loadPyodide({ indexURL: "pyodide/" });

  const src = await (await fetch("calc.py")).text();
  pyodide.runPython(src);
  pyCompute = pyodide.globals.get("compute");
  pyAtSpeed = pyodide.globals.get("at_speed");
  pyTireDiameter = pyodide.globals.get("tire_diameter");

  buildGearRows(DEFAULT_GEARS);
  wireEvents();
  recompute();

  $("#loading").hidden = true;
  $("#app").hidden = false;
}

main().catch((err) => {
  console.error(err);
  $("#loading").innerHTML =
    `<p role="alert">Failed to start Python.<br><small>${err}</small></p>`;
});
