/*
 * Gearing calculator frontend.
 *
 * All drivetrain math lives in calc.py and runs in the browser under Pyodide;
 * this file only gathers inputs, calls into Python, and draws SVG.
 */

const DEFAULT_GEARS = [3.6, 2.1, 1.4, 1.0, 0.8, 0.65];
const MAX_GEARS = 8;
const MIN_GEARS = 1;

const GEAR_COLORS = [
  "#2f6fed", "#e0803a", "#3aa76d", "#d64545",
  "#8c5bd8", "#0f9bb0", "#c74f9a", "#7a8b3f",
];

const $ = (sel) => document.querySelector(sel);
const gearColor = (i) => GEAR_COLORS[i % GEAR_COLORS.length];

let pyodide = null;
let pyCompute = null;
let pySpeedAtRpm = null;
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
 * Draw a round gauge: track arc, filled value arc, ticks, needle, readout.
 * `redlineAt` (a value, optional) shades the track from there to `max` in red.
 */
function renderGauge(svg, { value, max, step, label, unit, decimals = 0, redlineAt = null }) {
  const cx = 100;
  const cy = 92;
  const r = 66;
  const clamped = Math.max(0, Math.min(value, max));
  const angleFor = (v) => GAUGE_START + (max > 0 ? v / max : 0) * GAUGE_SWEEP;
  const valueAngle = angleFor(clamped);

  svg.replaceChildren();

  svg.append(svgEl("path", { class: "gauge-face", d: arcPath(cx, cy, r, GAUGE_START, GAUGE_END) }));

  if (redlineAt !== null && redlineAt < max) {
    svg.append(
      svgEl("path", { class: "gauge-redline", d: arcPath(cx, cy, r, angleFor(redlineAt), GAUGE_END) })
    );
  }

  if (clamped > 0) {
    svg.append(svgEl("path", { class: "gauge-arc", d: arcPath(cx, cy, r, GAUGE_START, valueAngle) }));
  }

  const big = max >= 1000; // label in thousands to keep the dial uncluttered
  for (let v = 0; v <= max + step / 1000; v += step) {
    const angle = angleFor(v);
    const [ix, iy] = polar(cx, cy, r - 9, angle);
    const [ox, oy] = polar(cx, cy, r - 2, angle);
    svg.append(svgEl("line", { class: "gauge-tick", x1: ix, y1: iy, x2: ox, y2: oy }));

    const [lx, ly] = polar(cx, cy, r - 21, angle);
    const shown = big ? v / 1000 : v;
    const txt = Number.isInteger(shown) ? String(shown) : shown.toFixed(1);
    svg.append(svgEl("text", { class: "gauge-tick-label", x: lx, y: ly + 3 }, txt));
  }

  const [nx, ny] = polar(cx, cy, r - 16, valueAngle);
  svg.append(svgEl("line", { class: "gauge-needle", x1: cx, y1: cy, x2: nx, y2: ny }));
  svg.append(svgEl("circle", { class: "gauge-hub", cx, cy, r: 3.5 }));

  // Readout sits below the dial's open bottom so it can't collide with ticks.
  svg.append(svgEl("text", { class: "gauge-value", x: cx, y: 152 }, clamped.toFixed(decimals)));
  svg.append(svgEl("text", { class: "gauge-label", x: cx, y: 169 }, `${label} · ${unit}`));
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
  const M = { top: 16, right: 16, bottom: 44, left: 52 };
  const plotW = W - M.left - M.right;
  const plotH = H - M.top - M.bottom;

  const xMax = result.max_rpm;
  const yMax = result.max_speed * 1.05 || 1;

  const xPix = (rpm) => M.left + (rpm / xMax) * plotW;
  const yPix = (spd) => M.top + plotH - (spd / yMax) * plotH;

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
      "Engine RPM")
  );
  svg.append(
    svgEl("text", {
      class: "chart-axis-title", x: 14, y: M.top + plotH / 2,
      "text-anchor": "middle", transform: `rotate(-90 14 ${M.top + plotH / 2})`,
    }, `Speed (${result.speed_unit})`)
  );

  // One polyline per gear.
  result.curves.forEach((curve, i) => {
    const pts = curve.samples.map(([rpm, spd]) => `${xPix(rpm)},${yPix(spd)}`).join(" ");
    svg.append(svgEl("polyline", { class: "chart-line", points: pts, stroke: gearColor(i) }));
  });

  // The acceleration run: up each gear to the shift RPM, then across to the
  // next gear at the same road speed. Drawn over the curves it annotates.
  if (result.trace.length) {
    const pts = result.trace.map(([rpm, spd]) => `${xPix(rpm)},${yPix(spd)}`).join(" ");
    svg.append(svgEl("polyline", { class: "chart-trace", points: pts }));
    result.shifts.forEach((s) => {
      svg.append(
        svgEl("circle", { class: "chart-shift", cx: xPix(result.shift_rpm), cy: yPix(s.speed), r: 3 })
      );
    });
  }

  // Marker for the currently selected gear + RPM.
  if (current && current.rpm > 0 && current.rpm <= xMax) {
    svg.append(
      svgEl("circle", {
        class: "chart-marker", cx: xPix(current.rpm), cy: yPix(current.speed), r: 4,
        fill: gearColor(current.gearIndex),
      })
    );
  }
}

function renderLegend(el, result) {
  el.replaceChildren();
  result.curves.forEach((curve, i) => {
    const span = document.createElement("span");
    const sw = document.createElement("i");
    sw.className = "swatch";
    sw.style.background = gearColor(i);
    span.append(sw, document.createTextNode(`Gear ${curve.gear} (${curve.ratio.toFixed(2)})`));
    el.append(span);
  });

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
    final_drive: num("#final_drive", 3.9),
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
    const sw = document.createElement("i");
    sw.className = "swatch";
    sw.style.background = gearColor(i);
    label.append(sw, document.createTextNode(String(i + 1)));

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

function syncGearSelect() {
  const select = $("#cur_gear");
  const n = gearInputs().length;
  const prev = parseInt(select.value, 10);
  select.replaceChildren();
  for (let i = 1; i <= n; i++) {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = String(i);
    select.append(opt);
  }
  select.value = String(prev >= 1 && prev <= n ? prev : Math.min(n, 1));
}

/* ------------------------------ python bridge ---------------------------- */

/** Recompute all gear curves via Python, then redraw everything. */
function recompute() {
  updateTireDiameter();
  const inputs = readInputs();
  if (inputs.gears.length === 0) return;

  const slider = $("#cur_rpm");
  slider.max = String(inputs.max_rpm);
  if (parseFloat(slider.value) > inputs.max_rpm) slider.value = String(inputs.max_rpm);

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

  document.querySelectorAll("[data-speed-unit]").forEach((el) => {
    el.textContent = lastResult.speed_unit;
  });

  redraw();
}

/** Redraw gauges/chart/table from `lastResult` + the current RPM & gear. */
function redraw() {
  if (!lastResult) return;
  const inputs = readInputs();
  const gearNum = parseInt($("#cur_gear").value, 10) || 1;
  const gearIndex = gearNum - 1;
  const ratio = inputs.gears[gearIndex];
  if (ratio === undefined) return;

  const rpm = parseFloat($("#cur_rpm").value);
  $("#cur_rpm_out").textContent = String(Math.round(rpm));

  const speed = pySpeedAtRpm(
    rpm, ratio, inputs.final_drive, inputs.transfer, inputs.tire, inputs.slip, inputs.units
  );

  const tachScale = gaugeScale(inputs.max_rpm);
  renderGauge($("#tach"), {
    value: rpm, ...tachScale, label: "RPM",
    unit: tachScale.max >= 1000 ? "×1000" : "rpm",
    redlineAt: inputs.max_rpm * 0.85,
  });

  const speedScale = gaugeScale(Math.max(lastResult.max_speed, 1));
  renderGauge($("#speedo"), {
    value: speed, ...speedScale, label: "Speed", unit: lastResult.speed_unit,
  });

  renderChart($("#chart"), lastResult, { rpm, speed, gearIndex });
  renderLegend($("#legend"), lastResult);
  renderTable($("#table tbody"), lastResult, inputs, gearNum);
  renderShiftTable($("#shift-table tbody"), lastResult);
}

/* ---------------------------------- init --------------------------------- */

function onUnitChange() {
  // The tire size itself is unit-agnostic; `recompute` re-derives the diameter
  // in the newly selected unit, so only the label needs updating here.
  document.querySelector("[data-tire-unit]").textContent =
    currentUnits() === "metric" ? "mm" : "in";
  recompute();
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

  // These only move the needles/marker; no need to recompute the curves.
  $("#cur_rpm").addEventListener("input", redraw);
  $("#cur_gear").addEventListener("change", redraw);

  $("#add-gear").addEventListener("click", () => {
    const ratios = gearInputs().map((el) => parseFloat(el.value) || 1);
    if (ratios.length >= MAX_GEARS) return;
    const last = ratios[ratios.length - 1] ?? 1;
    buildGearRows([...ratios, Math.max(0.1, +(last * 0.8).toFixed(2))]);
    syncGearSelect();
    recompute();
  });

  $("#remove-gear").addEventListener("click", () => {
    const ratios = gearInputs().map((el) => parseFloat(el.value) || 1);
    if (ratios.length <= MIN_GEARS) return;
    buildGearRows(ratios.slice(0, -1));
    syncGearSelect();
    recompute();
  });
}

async function main() {
  pyodide = await loadPyodide({ indexURL: "pyodide/" });

  const src = await (await fetch("calc.py")).text();
  pyodide.runPython(src);
  pyCompute = pyodide.globals.get("compute");
  pySpeedAtRpm = pyodide.globals.get("speed_at_rpm");
  pyTireDiameter = pyodide.globals.get("tire_diameter");

  buildGearRows(DEFAULT_GEARS);
  syncGearSelect();
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
