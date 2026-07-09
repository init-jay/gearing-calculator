"""Tests for the gearing calc module.

The module lives under ``app/static/`` because it is also served to and loaded
by Pyodide in the browser. We load it here by file path so this file is the
single source of truth for the math.
"""

import importlib.util
import sys
from math import isclose
from pathlib import Path

import pytest

CALC_PATH = Path(__file__).resolve().parent.parent / "app" / "static" / "calc.py"

spec = importlib.util.spec_from_file_location("calc", CALC_PATH)
calc = importlib.util.module_from_spec(spec)
assert spec and spec.loader
# @dataclass resolves annotations via sys.modules[cls.__module__], so the
# module must be registered before exec_module runs.
sys.modules["calc"] = calc
spec.loader.exec_module(calc)


def test_known_value_imperial():
    # 3000 rpm, direct gear 1.0, diff 3.9, 25 in tire, no slip -> ~57.2 mph
    mph = calc.speed_at_rpm(3000, 1.0, 3.9, 1.0, 25.0, slip=0.0, units="imperial")
    assert isclose(mph, 57.24, abs_tol=0.1)


def test_matches_336_identity():
    # mph = rpm * dia / (overall_ratio * 336.135)
    rpm, ratio, diff, tire = 4500, 1.4, 3.55, 26.0
    overall = ratio * diff * 1.0
    expected = rpm * tire / (overall * calc.MPH_CONST)
    got = calc.speed_at_rpm(rpm, ratio, diff, 1.0, tire, units="imperial")
    assert isclose(got, expected, rel_tol=1e-12)


def test_higher_gear_ratio_is_slower():
    # A numerically larger gear ratio (lower gear) => lower speed at same rpm.
    # Default units are metric, so the tire diameter is in mm.
    low = calc.speed_at_rpm(5000, 3.6, 3.9, 1.0, 635.0)
    high = calc.speed_at_rpm(5000, 0.8, 3.9, 1.0, 635.0)
    assert high > low


def test_slip_reduces_speed():
    base = calc.speed_at_rpm(4000, 1.0, 3.9, 1.0, 635.0, slip=0.0)
    slipped = calc.speed_at_rpm(4000, 1.0, 3.9, 1.0, 635.0, slip=0.1)
    assert isclose(slipped, base * 0.9, rel_tol=1e-12)


def test_rpm_at_speed_inverts_speed_at_rpm():
    args = dict(gear_ratio=1.4, final_drive=3.9, transfer=1.0, tire=25.0, slip=0.05)
    for units in ("imperial", "metric"):
        tire = 25.0 if units == "imperial" else 635.0
        a = dict(args, tire=tire, units=units)
        speed = calc.speed_at_rpm(3200, **a)
        back = calc.rpm_at_speed(speed, **a)
        assert isclose(back, 3200, rel_tol=1e-9)


def test_metric_consistency_with_imperial():
    # Same physical tire (25 in = 635 mm) should give the same physical speed.
    mph = calc.speed_at_rpm(3000, 1.0, 3.9, 1.0, 25.0, units="imperial")
    kmh = calc.speed_at_rpm(3000, 1.0, 3.9, 1.0, 25.0 * 25.4, units="metric")
    assert isclose(kmh, mph * 1.609344, rel_tol=1e-9)


def test_gear_table_shapes():
    inputs = calc.Inputs(gears=[3.6, 2.1, 1.4, 1.0, 0.8, 0.65], max_rpm=7000)
    result = calc.gear_table(inputs, step=1000)
    assert len(result.curves) == 6
    assert result.speed_unit == "km/h"
    # Every curve ends exactly at max_rpm.
    for curve in result.curves:
        assert curve.samples[-1][0] == 7000
        assert isclose(curve.samples[-1][1], curve.top_speed)
    # Top gear (smallest ratio) yields the max speed.
    assert isclose(result.max_speed, result.curves[-1].top_speed)


def test_compute_dict_roundtrip():
    out = calc.compute(
        {"gears": [3.6, 1.0], "final_drive": 3.9, "tire": 635, "max_rpm": 6000},
        step=2000,
    )
    assert out["speed_unit"] == "km/h"
    assert len(out["curves"]) == 2
    assert out["curves"][0]["gear"] == 1
    # from_dict drops non-positive gear ratios
    out2 = calc.compute({"gears": [3.6, 0, -1, 1.0]})
    assert len(out2["curves"]) == 2


def test_compute_dict_honours_explicit_imperial():
    out = calc.compute({"gears": [1.0], "tire": 25.0, "units": "imperial"})
    assert out["speed_unit"] == "mph"


def test_defaults_are_metric():
    # Locks in the metric default so flipping it back is a deliberate change.
    inputs = calc.Inputs(gears=[1.0])
    assert inputs.units == "metric"
    assert isclose(inputs.tire, calc.DEFAULT_TIRE_MM)
    assert calc.compute({"gears": [1.0]})["speed_unit"] == "km/h"


def test_tire_diameter_known_sizes():
    # 225/45R17: 17 in rim = 431.8 mm, plus two 101.25 mm sidewalls.
    assert isclose(calc.tire_diameter(225, 45, 17), 634.3, abs_tol=1e-9)
    assert isclose(calc.tire_diameter(225, 35, 17), 589.3, abs_tol=1e-9)
    assert isclose(calc.tire_diameter(*calc.DEFAULT_TIRE_SIZE), calc.DEFAULT_TIRE_MM)


def test_tire_diameter_imperial_is_same_physical_tire():
    mm = calc.tire_diameter(225, 45, 17, units="metric")
    inches = calc.tire_diameter(225, 45, 17, units="imperial")
    assert isclose(inches, mm / 25.4, rel_tol=1e-12)


def test_tire_diameter_feeds_speed_consistently():
    # A tire size drives the same physical speed through either unit system.
    kmh = calc.speed_at_rpm(3000, 1.0, 3.9, 1.0, calc.tire_diameter(225, 45, 17))
    mph = calc.speed_at_rpm(
        3000, 1.0, 3.9, 1.0, calc.tire_diameter(225, 45, 17, "imperial"), units="imperial"
    )
    assert isclose(kmh, mph * 1.609344, rel_tol=1e-9)


def test_tire_diameter_rejects_bad_input():
    with pytest.raises(ValueError):
        calc.tire_diameter(225, 45, 17, units="furlongs")
    for bad in ((0, 45, 17), (225, 0, 17), (225, 45, 0), (-225, 45, 17)):
        with pytest.raises(ValueError):
            calc.tire_diameter(*bad)
