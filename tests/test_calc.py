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


# --------------------------------- shifts ---------------------------------


def test_shift_lands_at_ratio_scaled_rpm():
    # 2.0 -> 1.0 halves the ratio, so the engine halves its rpm.
    (shift,) = calc.shift_points(calc.Inputs(gears=[2.0, 1.0], shift_rpm=6000))
    assert shift.from_gear == 1 and shift.to_gear == 2
    assert isclose(shift.rpm_after, 3000.0, rel_tol=1e-12)
    assert isclose(shift.rpm_drop, 3000.0, rel_tol=1e-12)


def test_speed_is_continuous_across_the_shift():
    # The property the whole trace rests on: the wheels don't change speed, so
    # the landing rpm must reproduce the shift speed in the next gear.
    inputs = calc.Inputs(gears=[3.6, 2.1, 1.4], slip=0.07, shift_rpm=6200)
    for shift in calc.shift_points(inputs):
        landed = calc.speed_at_rpm(
            shift.rpm_after,
            inputs.gears[shift.to_gear - 1],
            inputs.final_drive,
            inputs.transfer,
            inputs.tire,
            inputs.slip,
            inputs.units,
        )
        assert isclose(landed, shift.speed, rel_tol=1e-9)


def test_shift_rpm_is_independent_of_slip_and_tire():
    # rpm_after = shift_rpm * ratio_next / ratio_current; everything else cancels.
    base = calc.shift_points(calc.Inputs(gears=[3.6, 2.1], shift_rpm=6000))[0]
    for variant in (
        calc.Inputs(gears=[3.6, 2.1], shift_rpm=6000, slip=0.3),
        calc.Inputs(gears=[3.6, 2.1], shift_rpm=6000, tire=800.0),
        calc.Inputs(gears=[3.6, 2.1], shift_rpm=6000, final_drive=2.5, transfer=1.7),
    ):
        assert isclose(calc.shift_points(variant)[0].rpm_after, base.rpm_after, rel_tol=1e-9)


def test_shift_rpm_is_unit_independent():
    # Same physical car described two ways: engine rpm is a property of the
    # gearbox, so only the road speed column may differ.
    #
    # Equality here is *exact*, not approximate. Deriving rpm_after by dividing
    # the shift speed back out through the tire leaves ~1e-13 of unit-dependent
    # float noise, which is enough to round 1312.5 rpm to 1313 in one unit
    # system and 1312 in the other. Keep this assertion exact so that
    # regression cannot come back.
    gears = [3.6, 2.1, 1.4, 1.0, 0.8, 0.65]
    metric = calc.shift_points(calc.Inputs(gears=gears, tire=635.0, shift_rpm=7000))
    imperial = calc.shift_points(
        calc.Inputs(gears=gears, tire=25.0, units="imperial", shift_rpm=7000)
    )
    for m, i in zip(metric, imperial):
        assert m.rpm_after == i.rpm_after
        assert m.rpm_drop == i.rpm_drop
        assert isclose(m.speed, i.speed * 1.609344, rel_tol=1e-9)

    # The 5->6 pair is the one that actually tripped the rounding bug.
    assert metric[-1].rpm_drop == 1312.5


def test_shift_count_is_one_less_than_gears():
    assert len(calc.shift_points(calc.Inputs(gears=[3.6, 2.1, 1.4, 1.0, 0.8, 0.65]))) == 5
    assert calc.shift_points(calc.Inputs(gears=[3.0])) == []


def test_badly_ordered_gears_report_a_negative_drop():
    # A "taller" first gear is a downshift, not an upshift. Surface it.
    (shift,) = calc.shift_points(calc.Inputs(gears=[1.0, 2.0], shift_rpm=3000))
    assert shift.rpm_drop < 0
    assert isclose(shift.rpm_after, 6000.0, rel_tol=1e-12)


def test_shift_rpm_defaults_to_redline_and_clamps():
    assert calc.Inputs(gears=[1.0], max_rpm=6500).effective_shift_rpm() == 6500
    assert calc.Inputs(gears=[1.0], max_rpm=6500, shift_rpm=5000).effective_shift_rpm() == 5000
    # Shifting above the redline is not a thing.
    assert calc.Inputs(gears=[1.0], max_rpm=6500, shift_rpm=9000).effective_shift_rpm() == 6500
    with pytest.raises(ValueError):
        calc.Inputs(gears=[1.0], shift_rpm=0).effective_shift_rpm()


# ---------------------------------- trace ----------------------------------


def test_trace_shape_and_monotonic_speed():
    gears = [3.6, 2.1, 1.4, 1.0, 0.8, 0.65]
    inputs = calc.Inputs(gears=gears, max_rpm=7000, shift_rpm=6500)
    trace = calc.shift_trace(inputs)

    # Origin, then two points per gear minus the top gear's absent upshift.
    assert len(trace) == 2 * len(gears)
    assert trace[0] == (0.0, 0.0)
    assert trace[-1][0] == 7000  # top gear runs out to the redline, not the shift point

    speeds = [s for _, s in trace]
    assert speeds == sorted(speeds)  # never lose road speed


def test_trace_jumps_backwards_in_rpm_at_each_shift():
    inputs = calc.Inputs(gears=[3.6, 2.1, 1.4], shift_rpm=6000)
    trace = calc.shift_trace(inputs)
    # Points 1..n pair up as (shift, landing): same speed, strictly lower rpm.
    for shift_pt, landing in zip(trace[1::2], trace[2::2]):
        assert isclose(shift_pt[1], landing[1], rel_tol=1e-9)  # speed held
        assert landing[0] < shift_pt[0]  # rpm dropped


def test_trace_of_single_gear_is_a_straight_line():
    trace = calc.shift_trace(calc.Inputs(gears=[3.0], max_rpm=7000))
    assert len(trace) == 2
    assert trace[0] == (0.0, 0.0)
    assert trace[1][0] == 7000


def test_compute_exposes_shifts_and_trace():
    out = calc.compute({"gears": [3.6, 2.1, 1.0], "shift_rpm": 6000, "max_rpm": 7000})
    assert out["shift_rpm"] == 6000
    assert len(out["shifts"]) == 2
    first = out["shifts"][0]
    assert (first["from_gear"], first["to_gear"]) == (1, 2)
    assert isclose(first["rpm_after"], 3500.0)  # 6000 * 2.1 / 3.6
    assert isclose(first["rpm_drop"], 2500.0)
    assert len(out["trace"]) == 6
    # Omitting shift_rpm falls back to the redline.
    assert calc.compute({"gears": [1.0], "max_rpm": 6800})["shift_rpm"] == 6800


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
