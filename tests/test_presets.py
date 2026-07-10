"""Validate the shipped presets against the invariants the frontend assumes.

The app reads ``presets.json`` and drops its contents straight into the input
form, so a malformed entry surfaces as a broken chart rather than an error. These
tests are the guardrail for anyone adding a gearbox, an engine, a vehicle, or a
tire to that file.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

spec = importlib.util.spec_from_file_location("calc", STATIC / "calc.py")
calc = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["calc"] = calc
spec.loader.exec_module(calc)

PRESETS = json.loads((STATIC / "presets.json").read_text())
GEARBOXES = PRESETS["gearboxes"]
ENGINES = PRESETS["engines"]
VEHICLES = PRESETS["vehicles"]
TIRES = PRESETS["tires"]


def test_the_file_carries_a_note_about_what_it_is():
    # These are archetypes, not measured data. Say so in the file itself.
    assert "not specifications" in PRESETS["note"]


def test_there_is_something_to_choose_from():
    assert len(GEARBOXES) >= 2
    assert len(ENGINES) >= 2
    assert len(VEHICLES) >= 2
    assert len(TIRES) >= 2


@pytest.mark.parametrize("group", ["gearboxes", "engines", "vehicles", "tires"])
def test_names_are_unique(group):
    names = [item["name"] for item in PRESETS[group]]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("box", GEARBOXES, ids=lambda b: b["name"])
def test_gearbox_ratios_descend(box):
    # `shift_points` reads the list in order and reports a negative rpm drop for
    # an out-of-order pair. A preset must never be the thing that triggers that.
    gears = box["gears"]
    assert gears == sorted(gears, reverse=True)
    assert len(set(gears)) == len(gears)


@pytest.mark.parametrize("box", GEARBOXES, ids=lambda b: b["name"])
def test_gearbox_fits_the_form(box):
    assert 1 <= len(box["gears"]) <= 8  # MAX_GEARS in app.js
    assert all(g > 0 for g in box["gears"])


@pytest.mark.parametrize("engine", ENGINES, ids=lambda e: e["name"])
def test_engine_curve_fits_the_form(engine):
    curve = engine["torque_curve"]
    assert 2 <= len(curve) <= 12  # MAX_CURVE_POINTS in app.js
    assert all(rpm > 0 and torque > 0 for rpm, torque in curve)


@pytest.mark.parametrize("engine", ENGINES, ids=lambda e: e["name"])
def test_engine_curve_rpm_strictly_increases(engine):
    rpms = [rpm for rpm, _ in engine["torque_curve"]]
    assert rpms == sorted(rpms)
    assert len(set(rpms)) == len(rpms)


@pytest.mark.parametrize("engine", ENGINES, ids=lambda e: e["name"])
def test_engine_curve_reaches_its_redline(engine):
    # `curve_span` clips at the redline, so a curve that stops short would draw
    # tractive-effort curves that end before the engine does.
    assert engine["torque_curve"][-1][0] >= engine["redline"]


@pytest.mark.parametrize("engine", ENGINES, ids=lambda e: e["name"])
def test_engine_curve_starts_below_its_redline(engine):
    assert engine["torque_curve"][0][0] < engine["redline"]


@pytest.mark.parametrize("engine", ENGINES, ids=lambda e: e["name"])
def test_every_engine_drives_every_gearbox(engine):
    # The cross product is what the two dropdowns actually let a user build.
    for box in GEARBOXES:
        result = calc.compute({
            "gears": box["gears"],
            "max_rpm": engine["redline"],
            "shift_rpm": engine["redline"],
            "torque_curve": engine["torque_curve"],
        })
        assert len(result["efforts"]) == len(box["gears"])
        assert result["max_force"] > 0
        assert len(result["crossovers"]) == len(box["gears"]) - 1
        assert result["peak_power"][1] > 0


def test_at_least_one_preset_pairing_shifts_before_the_redline():
    # If every combination shifted at the limiter, the optimal-shift table would
    # be a column of "(redline)" and the feature would teach nothing.
    early = 0
    for engine in ENGINES:
        for box in GEARBOXES:
            inputs = calc.Inputs.from_dict({
                "gears": box["gears"],
                "max_rpm": engine["redline"],
                "torque_curve": engine["torque_curve"],
            })
            early += sum(not c.at_redline for c in calc.shift_crossovers(inputs))
    assert early > 0


@pytest.mark.parametrize("vehicle", VEHICLES, ids=lambda v: v["name"])
def test_vehicle_weight_is_plausible(vehicle):
    # Always kg, never lb — the app converts for display. A preset entered in lb
    # would read as a featherweight car and silently halve every 0-100 time.
    assert 500 <= vehicle["weight"] <= 4000


@pytest.mark.parametrize("tire", TIRES, ids=lambda t: t["name"])
def test_tire_grip_is_plausible(tire):
    # Lateral G. Below ~0.5 nothing road-legal, above ~2.0 nothing without wings.
    assert 0.5 <= tire["grip"] <= 2.0


def test_every_vehicle_and_tire_reaches_the_benchmark():
    # The cross product the two new dropdowns let a user build, on the strongest
    # preset engine. A pairing that never reaches 100 km/h would show "—" forever.
    engine = max(ENGINES, key=lambda e: max(t for _, t in e["torque_curve"]))
    for vehicle in VEHICLES:
        for tire in TIRES:
            result = calc.compute({
                "gears": GEARBOXES[0]["gears"],
                "max_rpm": engine["redline"],
                "shift_rpm": engine["redline"],
                "torque_curve": engine["torque_curve"],
                "weight": vehicle["weight"],
                "grip": tire["grip"],
            })
            accel = result["acceleration"]
            assert accel["time"] is not None
            assert accel["time"] > 0.0


def test_grip_and_weight_move_the_benchmark_in_the_right_direction():
    # Guards the wiring, not the physics: a preset that fed the wrong field would
    # still produce a number, just an unresponsive one.
    def run(weight, grip):
        return calc.compute({
            "gears": GEARBOXES[0]["gears"], "torque_curve": ENGINES[0]["torque_curve"],
            "max_rpm": ENGINES[0]["redline"], "weight": weight, "grip": grip,
        })["acceleration"]["time"]

    heaviest = max(v["weight"] for v in VEHICLES)
    lightest = min(v["weight"] for v in VEHICLES)
    assert run(heaviest, 1.0) > run(lightest, 1.0)

    stickiest = max(t["grip"] for t in TIRES)
    slipperiest = min(t["grip"] for t in TIRES)
    assert run(1400.0, stickiest) < run(1400.0, slipperiest)
