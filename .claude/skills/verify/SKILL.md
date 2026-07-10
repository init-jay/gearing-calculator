---
name: verify
description: Build, launch, and drive the gearing-calculator app to observe a change working end-to-end in a real browser.
---

# Verifying gearing-calculator changes

## Launch

A dev server is often **already running on port 8000** and serves `app/static/`
straight from disk — check `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/`
before starting your own. If nothing answers:

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000   # background it
```

`config.json` may point Pyodide at the CDN (`"source": "cdn"`), so first page
load needs the network and takes ~10-30 s while the WASM runtime boots. Wait
for `#app:not([hidden])` (timeout ≥ 120 s), not for `load`.

## Drive (headless browser)

Playwright browsers are pre-cached in `~/.cache/ms-playwright`; the package is
not installed. Run scripts with:

```bash
uv run --with playwright python your_probe.py
```

Gotchas learned the hard way:

- The page is tall. `scroll_into_view_if_needed()` before `mouse.move` —
  coordinates below the viewport silently produce no pointer events.
- Upload files with `page.set_input_files("#lap-file", path)`.
- Capture console errors via `page.on("console", ...)` and `page.on("pageerror", ...)`;
  a clean run has none.
- Dark mode: `browser.new_page(color_scheme="dark")`; flip live with
  `page.emulate_media(color_scheme="light")` to exercise scheme-change handlers.

## Flows worth driving

- Gearing math: edit inputs in the Inputs panel, watch gauges/charts redraw.
- Lap map: upload a RaceChrono Format-3 CSV (generate a synthetic one — kidney
  loop of `lat0 + r·sin(a)`, `lon0 + r·cos(a)/cos(lat0)` rows at 20 Hz with the
  real 19-column header including duplicate `speed` columns; derive
  `lateral_acc` from path curvature — v²·κ/9.81 — or the corner-freeze gear
  logic never triggers), then check
  `#lap-view` unhides, `.lap-trace line` count, `#lap-select` options,
  `#lap-legend` text, hover readout, and a garbage upload showing `#lap-error`.
- Lap gear maps: check `input[name="lap-color"][value="gear"]`, then `#compare`
  → two figures in `#lap-maps` captioned Setup A/B. Gearbox preset selects are
  `#setup-b select[data-preset="gearbox"]` — note option index 1 is preset "0"
  (the seeded default, index 0 is "Custom"), so pick value "2"+ to actually
  change B. Diff the two maps' `.lap-trace line` stroke arrays to prove the
  gearsets diverge.

## Unit tests (CI's job, not verification)

`uv run pytest -q` from the repo root — running it from elsewhere finds no tests.
