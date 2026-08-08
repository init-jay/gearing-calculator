# Gearing Calculator

A modern vehicle gearing analyzer, inspired by
[blocklayer.com/rpm-gear](https://www.blocklayer.com/rpm-gear).

Have a play on **[gears.kranky.dev](https://gears.kranky.dev/)** (a hosted copy of this repo, no setup required).

![Gearing Calculator screenshot](docs/hero.png)


Also available as a native iOS app: **[ThirdGear on Apple App Store](https://apps.apple.com/au/app/thirdgear/id6791802366)**. 

## Features

Relates engine RPM to road speed through the drivetrain — transmission gear
ratio × final drive × transfer case, with tire diameter and torque-converter
slip — and shows a tachometer, speedometer, a chart of engine RPM against road
speed with one line per gear, a top-speed table, and a shift-point table.

Drag the speed slider to walk a whole acceleration run: the gear follows the
shift schedule and the tachometer sawtooths as each shift lands.

The gearing math is written in Python and **runs in the browser** via
[Pyodide](https://pyodide.org) (WebAssembly). The FastAPI server only serves
static files. By default the Pyodide runtime is served from this origin too —
**nothing is fetched from a CDN**, so the site works fully offline. That is a
[configurable choice](#where-pyodide-comes-from), not a fact of the build.

## Setup

Vendor the Pyodide runtime once (~12 MB, gitignored):

```bash
uv run scripts/vendor_pyodide.py
```

Skip this if you set `pyodide.source` to `"cdn"` — see below.

## Where Pyodide comes from

`app/static/config.json` decides. It is also the only place the Pyodide version
is written down: the browser reads it to build the runtime URL, and
`scripts/vendor_pyodide.py` reads it to know what to download, so a vendored copy
and a CDN copy cannot end up on different releases.

```json
{ "pyodide": { "source": "vendored", "version": "0.28.0", ... } }
```

| `source` | Runtime comes from | Trade-off |
| --- | --- | --- |
| `"vendored"` (default) | `app/static/pyodide/`, same origin | Works with no network. The deploy carries ~12 MB and you must vendor first. |
| `"cdn"` | `cdn.jsdelivr.net` | Deploy carries no runtime and needs no vendoring step. Every load needs the network, and a third party serves code that executes in the page. |

Under `"cdn"` you can delete `app/static/pyodide/` entirely — nothing else
references it. Bump `version` once to move both.

There is deliberately no "CDN with a local fallback". A fallback would mean a
deploy that looks offline-capable under test and is not, or that silently ships
12 MB nobody asked for. Pick one; it fails loudly if it can't load.

## Run

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then open http://127.0.0.1:8000. Or run the module directly:

```bash
uv run python -m app.main   # same thing, with --reload
```

## Test

```bash
uv run pytest
```

## Layout

| Path | Purpose |
| --- | --- |
| `app/static/calc.py` | All drivetrain math. Pure stdlib Python. |
| `app/static/app.js` | Boots Pyodide, wires the form, draws the SVG gauges/chart. |
| `app/static/index.html`, `styles.css` | Page structure and styling. |
| `app/static/config.json` | Where Pyodide loads from, and the one pinned version. |
| `app/static/presets.json` | Gearbox ratios and engine torque curves for the dropdowns. |
| `app/static/favicon.ico`, `favicon.svg` | Gear icon, generated — edit the profile, not the files. |
| `app/main.py` | FastAPI static-file server. |
| `scripts/vendor_pyodide.py` | Downloads the Pyodide runtime pinned by `config.json`. |
| `scripts/make_favicon.py` | Draws both favicons from one gear profile (stdlib only). |
| `tests/test_calc.py` | Unit tests for `calc.py`. |
| `tests/test_config.py`, `tests/test_presets.py` | Guard the two data files. |
| `tests/test_favicon.py` | Checks the icons still match the script that draws them. |

`calc.py` is the single source of truth for the math: the browser loads it into
Pyodide, and `tests/test_calc.py` imports the same file directly, so the
formulas are verifiable without a browser.

## The model

Metric is the default:

```
overall_ratio = gear_ratio × final_drive × transfer_case
speed_kmh     = rpm × π × tire_diameter_mm / overall_ratio × (1 − slip) × 60 / 1e6
```

Selecting imperial units swaps the tire diameter to inches and the output to mph:

```
speed_mph     = rpm × tire_diameter_in / (overall_ratio × 336.135) × (1 − slip)
```

where `336.135 = 63360 / (π × 60)` converts inches-per-minute to miles-per-hour.

Tire diameter is derived from a standard tire size — `225/45R17` is a 225 mm
section width, a sidewall 45% of that width, on a 17 in wheel — with the
sidewall counted once above the rim and once below:

```
tire_diameter_mm = wheel_diameter_in × 25.4 + 2 × section_width_mm × aspect / 100
```

## Shift points

Shifting does not change road speed — the clutch reconnects the same wheels at
the same speed — so the engine drops to whatever RPM the next gear needs to hold
that speed:

```
rpm_after_shift = rpm_shift × ratio_next / ratio_current
```

Tire diameter, final drive, transfer case and converter slip all cancel out of
that expression: the RPM drop across a shift is a property of the gearbox alone,
and is identical in metric and imperial. Only the road speed at which the shift
happens depends on the rest of the drivetrain.

The chart overlays a **shift trace** — the engine's path through a full
acceleration run, climbing each gear to the shift RPM and then dropping straight
down to the next gear at the same road speed. Plotting RPM against speed (rather
than the reverse) makes this a sawtooth: road speed only ever increases through a
run, so it parameterises the whole path, while a given RPM occurs once per gear.

## Standing start

Given a vehicle weight and a tire grip figure, the calculator times a run from
rest to 100 km/h — or to 60 mph under imperial units, each being the benchmark
its readers know. The answer is in seconds either way.

At every road speed the force reaching the road is the smaller of what the engine
can send there and what the tires can hold:

```
F_engine = tractive_effort(gear at this speed)
F_grip   = grip_in_G × mass × 9.80665
a        = min(F_engine, F_grip) / (mass × spin)
```

`spin` is the drivetrain's own rotational inertia, expressed as an equivalent
mass: `1.04 + 0.0025 × overall_ratio²`. It grows with the square of the ratio,
which is why a shorter gear is not the free acceleration a plain `F = ma` would
promise. Integrating `dv / a` from rest to the target, and adding the shift dead
time once per upshift, gives the time.

Two consequences are worth knowing before reading a number off it. In the
traction-limited region the mass cancels — `grip × m × g / (m × spin)` — so it is
grip, not weight, that sets the launch; weight only starts to matter once the
engine, rather than the tires, is the limit. And taking the whole weight as load
on the driven axle ignores weight distribution and transfer, so the grip cap is
an optimistic bound rather than a launch model.

**What is not modelled:** aerodynamic drag, rolling resistance, drivetrain
efficiency losses, and the torque converter. Each needs data the calculator never
asks for — a frontal area and drag coefficient, a rolling-resistance coefficient,
an efficiency map, a converter's K-factor — and inventing defaults for them would
dress a gearing tool up as a vehicle-dynamics sim. Since nothing decelerates the
car during a shift, dead time simply adds.

Absolute times are therefore optimistic. Comparing two gearsets on one car —
which is what this calculator is for — they are sound.
