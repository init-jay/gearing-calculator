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


def test_default_final_drive():
    assert calc.Inputs(gears=[1.0]).final_drive == 3.64
    assert calc.compute({"gears": [1.0]})["curves"][0]["ratio"] == 1.0
    # from_dict must use the same default as the dataclass.
    assert calc.Inputs.from_dict({"gears": [1.0]}).final_drive == 3.64


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


# ------------------------------ gear at speed ------------------------------

STOCK = [5.14, 2.83, 1.79, 1.26, 1.0, 0.83]


def test_gear_at_speed_walks_up_through_the_gears():
    inputs = calc.Inputs(gears=STOCK)
    shifts = calc.shift_points(inputs)

    assert calc.gear_at_speed(inputs, 0.0) == 1
    # Just below the first shift you are still in first.
    assert calc.gear_at_speed(inputs, shifts[0].speed - 0.001) == 1
    # Beyond the last shift you are in top gear and stay there.
    assert calc.gear_at_speed(inputs, shifts[-1].speed + 1) == len(STOCK)
    assert calc.gear_at_speed(inputs, 10_000) == len(STOCK)


def test_gear_at_exactly_the_shift_speed_is_the_next_gear():
    # At the shift speed the shift has just happened.
    inputs = calc.Inputs(gears=STOCK)
    for shift in calc.shift_points(inputs):
        assert calc.gear_at_speed(inputs, shift.speed) == shift.to_gear


def test_gear_at_speed_single_gear_and_empty():
    assert calc.gear_at_speed(calc.Inputs(gears=[3.0]), 500.0) == 1
    with pytest.raises(ValueError):
        calc.gear_at_speed(calc.Inputs(gears=[]), 10.0)


def test_lower_shift_rpm_moves_gear_boundaries_to_lower_speeds():
    late = calc.Inputs(gears=STOCK, shift_rpm=7000)
    early = calc.Inputs(gears=STOCK, shift_rpm=4000)
    for a, b in zip(calc.shift_points(late), calc.shift_points(early)):
        assert b.speed < a.speed
    # A speed that is still 1st gear when winding out is already 2nd if you
    # short-shift.
    speed = calc.shift_points(early)[0].speed + 1
    assert calc.gear_at_speed(early, speed) == 2
    assert calc.gear_at_speed(late, speed) == 1


# -------------------------------- at_speed ---------------------------------


def test_at_speed_lands_exactly_on_each_shift_point():
    # The property that puts the chart marker *on* the trace: at a shift speed,
    # at_speed must report the gear just engaged, at that shift's rpm_after.
    data = {"gears": STOCK}
    inputs = calc.Inputs.from_dict(data)
    for shift in calc.shift_points(inputs):
        got = calc.at_speed(data, shift.speed)
        assert got["gear"] == shift.to_gear
        assert isclose(got["rpm"], shift.rpm_after, rel_tol=1e-9)


def test_at_speed_rpm_matches_rpm_at_speed_for_the_derived_gear():
    data = {"gears": STOCK, "slip": 0.05}
    inputs = calc.Inputs.from_dict(data)
    for speed in (5.0, 30.0, 70.0, 120.0, 200.0, 260.0):
        got = calc.at_speed(data, speed)
        assert got["ratio"] == STOCK[got["gear"] - 1]
        expected = calc.rpm_at_speed(
            speed, got["ratio"], inputs.final_drive, inputs.transfer,
            inputs.tire, inputs.slip, inputs.units,
        )
        assert isclose(got["rpm"], expected, rel_tol=1e-12)


def test_at_speed_picks_the_same_gear_in_either_unit_system():
    # 100 km/h and 62.137 mph are the same road speed, so the same gear.
    kmh = calc.at_speed({"gears": STOCK, "tire": 635.0}, 100.0)
    mph = calc.at_speed(
        {"gears": STOCK, "tire": 25.0, "units": "imperial"}, 100.0 / 1.609344
    )
    assert kmh["gear"] == mph["gear"]
    assert isclose(kmh["rpm"], mph["rpm"], rel_tol=1e-9)


def test_at_speed_rpm_rises_within_a_gear_and_drops_across_a_shift():
    # The sawtooth, sampled: rpm climbs with speed until a shift, then falls.
    data = {"gears": STOCK}
    inputs = calc.Inputs.from_dict(data)
    boundary = calc.shift_points(inputs)[0].speed

    before_lo = calc.at_speed(data, boundary * 0.5)
    before_hi = calc.at_speed(data, boundary - 0.001)
    after = calc.at_speed(data, boundary)

    assert before_lo["gear"] == before_hi["gear"] == 1
    assert before_hi["rpm"] > before_lo["rpm"]  # climbing in gear
    assert after["gear"] == 2
    assert after["rpm"] < before_hi["rpm"]  # dropped on the shift


# --------------------------- tractive effort ------------------------------

# A gentle hump: peak torque at 4000, and torque falls slowly enough past it that
# power is still climbing at the limiter.
CURVE = [(1000, 180.0), (2000, 220.0), (3000, 245.0), (4000, 255.0),
         (5000, 250.0), (6000, 235.0), (7000, 205.0)]

# Torque collapses past 5000, so power peaks around there and falls away after.
PEAKY = [(1000, 180.0), (3000, 245.0), (4000, 255.0), (5000, 250.0),
         (6000, 200.0), (7000, 140.0)]

# A close-ratio box: each upshift drops the revs by ~15%, not the ~45% STOCK does.
CLOSE = [1.5, 1.3, 1.13]


def test_power_matches_the_5252_identity():
    # hp and lb-ft cross at 5252 rpm, by construction of the constant.
    assert isclose(calc.power_at_rpm(300.0, 5252.113, "imperial"), 300.0, rel_tol=1e-6)


def test_power_metric_is_torque_times_angular_velocity():
    # kW = N.m * (2*pi*rpm/60) / 1000, computed the long way round.
    from math import pi as _pi
    torque, rpm = 255.0, 4000.0
    expected = torque * (2 * _pi * rpm / 60.0) / 1000.0
    assert isclose(calc.power_at_rpm(torque, rpm, "metric"), expected, rel_tol=1e-12)


def test_power_is_unit_system_consistent():
    # The same physical engine point, stated either way, is the same power.
    kw = calc.power_at_rpm(255.0, 4000.0, "metric")
    hp = calc.power_at_rpm(255.0 / calc.NM_PER_LBFT, 4000.0, "imperial")
    assert isclose(kw, hp * 0.745699872, rel_tol=1e-6)


def test_tractive_effort_matches_the_articles_worked_example():
    # 340 lb-ft through 3:1 and 4:1 is 4080 lb-ft of axle torque. On a 25 in
    # tire (radius 25/24 ft) that is 4080 / (25/24) = 3916.8 lbf.
    force = calc.tractive_effort(340.0, 3.0, 4.0, 1.0, 25.0, units="imperial")
    assert isclose(force, 340.0 * 12.0 / (25.0 / 24.0), rel_tol=1e-12)
    assert isclose(force, 3916.8, rel_tol=1e-9)


def test_tractive_effort_metric_newtons():
    # 255 N.m * 12 / 0.3 m radius = 10200 N on a 600 mm tire.
    force = calc.tractive_effort(255.0, 3.0, 4.0, 1.0, 600.0, units="metric")
    assert isclose(force, 255.0 * 12.0 / 0.3, rel_tol=1e-12)


def test_tractive_effort_is_unit_system_consistent():
    # Same engine, same car, stated metric vs imperial: 1 lbf = 4.4482216 N.
    metric = calc.tractive_effort(255.0, 3.0, 4.0, 1.0, 635.0, units="metric")
    imperial = calc.tractive_effort(
        255.0 / calc.NM_PER_LBFT, 3.0, 4.0, 1.0, 635.0 / calc.MM_PER_INCH, units="imperial"
    )
    assert isclose(metric, imperial * 4.4482216152605, rel_tol=1e-9)


def test_tractive_effort_falls_as_gears_get_taller():
    forces = [
        calc.tractive_effort(255.0, ratio, 3.64, 1.0, 634.3) for ratio in STOCK
    ]
    assert forces == sorted(forces, reverse=True)


def test_torque_at_rpm_interpolates_linearly():
    assert isclose(calc.torque_at_rpm(CURVE, 3500.0), 250.0)  # midway 245 -> 255
    assert isclose(calc.torque_at_rpm(CURVE, 3000.0), 245.0)  # exactly on a point


def test_torque_at_rpm_clamps_outside_the_curve():
    assert calc.torque_at_rpm(CURVE, 10.0) == 180.0
    assert calc.torque_at_rpm(CURVE, 99999.0) == 205.0


def test_normalize_curve_sorts_dedupes_and_drops_nonpositive():
    got = calc.normalize_curve([(3000, 245), (1000, 180), (1000, 190), (2000, 0), (-5, 100)])
    assert got == [(1000.0, 190.0), (3000.0, 245.0)]


def test_convert_curve_round_trips():
    there = calc.convert_curve(CURVE, "imperial")
    back = calc.convert_curve(there, "metric")
    assert all(isclose(a[1], b[1], rel_tol=1e-12) for a, b in zip(CURVE, back))
    assert isclose(there[0][1], 180.0 / calc.NM_PER_LBFT, rel_tol=1e-12)


def test_curve_span_is_bounded_by_the_redline():
    inputs = calc.Inputs.from_dict({"gears": STOCK, "torque_curve": CURVE, "max_rpm": 5500})
    assert inputs.curve_span() == (1000.0, 5500.0)


def test_curve_span_is_none_without_enough_points():
    assert calc.Inputs.from_dict({"gears": STOCK}).curve_span() is None
    assert calc.Inputs.from_dict({"gears": STOCK, "torque_curve": [(1000, 180)]}).curve_span() is None


def test_no_torque_curve_means_no_effort_output():
    result = calc.compute({"gears": STOCK})
    assert result["efforts"] == []
    assert result["crossovers"] == []
    assert result["engine"] == []
    assert result["max_force"] == 0.0
    assert result["peak_power"] is None


def test_effort_curves_never_leave_the_curve_span():
    inputs = calc.Inputs.from_dict({"gears": STOCK, "torque_curve": CURVE, "max_rpm": 6500})
    low, high = inputs.curve_span()
    for curve in calc.effort_curves(inputs):
        speeds = [s for s, _ in curve.samples]
        assert isclose(speeds[0], calc._speed(inputs, low, curve.ratio), rel_tol=1e-9)
        assert isclose(speeds[-1], calc._speed(inputs, high, curve.ratio), rel_tol=1e-9)


def test_effort_curve_force_matches_the_formula_pointwise():
    inputs = calc.Inputs.from_dict({"gears": STOCK, "torque_curve": CURVE})
    first = calc.effort_curves(inputs)[0]
    speed, force = first.samples[0]
    rpm = calc._rpm(inputs, speed, first.ratio)
    expected = calc.torque_at_rpm(CURVE, rpm) * first.ratio * 3.64 / (0.6343 / 2)
    assert isclose(force, expected, rel_tol=1e-9)


def test_peak_power_sits_above_peak_torque_rpm():
    # Torque falls slowly past its peak, so power keeps climbing for a while.
    result = calc.compute({"gears": STOCK, "torque_curve": CURVE})
    assert result["peak_torque"][0] == 4000.0
    assert result["peak_power"][0] > result["peak_torque"][0]


def test_crossovers_are_one_per_gear_pair():
    result = calc.compute({"gears": STOCK, "torque_curve": CURVE})
    crossings = result["crossovers"]
    assert [(c["from_gear"], c["to_gear"]) for c in crossings] == [
        (1, 2), (2, 3), (3, 4), (4, 5), (5, 6)
    ]


def test_at_the_crossover_both_gears_pull_equally():
    inputs = calc.Inputs.from_dict({"gears": CLOSE, "torque_curve": PEAKY})
    for cross in calc.shift_crossovers(inputs):
        assert not cross.at_redline
        a = inputs.gears[cross.from_gear - 1]
        b = inputs.gears[cross.to_gear - 1]
        fa = calc._effort(inputs, a, calc._rpm(inputs, cross.speed, a))
        fb = calc._effort(inputs, b, calc._rpm(inputs, cross.speed, b))
        assert isclose(fa, fb, rel_tol=1e-6)


def test_at_the_crossover_power_before_equals_power_after():
    # Both gears are at the same road speed, and force * speed is power, so equal
    # force means equal power. The optimal upshift is where the revs you drop to
    # make exactly as much power as the revs you are leaving.
    inputs = calc.Inputs.from_dict({"gears": CLOSE, "torque_curve": PEAKY})
    for cross in calc.shift_crossovers(inputs):
        before = calc.power_at_rpm(calc.torque_at_rpm(PEAKY, cross.rpm), cross.rpm)
        after = calc.power_at_rpm(calc.torque_at_rpm(PEAKY, cross.rpm_after), cross.rpm_after)
        assert isclose(before, after, rel_tol=1e-6)


def test_below_the_crossover_the_lower_gear_pulls_harder():
    inputs = calc.Inputs.from_dict({"gears": CLOSE, "torque_curve": PEAKY})
    low = inputs.curve_span()[0]
    for cross in calc.shift_crossovers(inputs):
        a = inputs.gears[cross.from_gear - 1]
        b = inputs.gears[cross.to_gear - 1]
        # Just inside the overlap, before the crossing.
        v = max(cross.speed * 0.98, calc._speed(inputs, low, b) * 1.001)
        if v >= cross.speed:
            continue
        assert calc._effort(inputs, a, calc._rpm(inputs, v, a)) > calc._effort(
            inputs, b, calc._rpm(inputs, v, b)
        )


def test_crossover_rpm_after_is_the_ratio_drop():
    inputs = calc.Inputs.from_dict({"gears": STOCK, "torque_curve": CURVE})
    for cross in calc.shift_crossovers(inputs):
        a = inputs.gears[cross.from_gear - 1]
        b = inputs.gears[cross.to_gear - 1]
        assert isclose(cross.rpm_after, cross.rpm * b / a, rel_tol=1e-12)


def test_a_still_climbing_engine_shifts_at_the_redline():
    # Torque rising all the way to the limiter: the lower gear never gives up.
    rising = [(1000, 100.0), (7000, 400.0)]
    inputs = calc.Inputs.from_dict({"gears": [3.0, 2.0], "torque_curve": rising})
    cross = calc.shift_crossovers(inputs)[0]
    assert cross.at_redline
    assert isclose(cross.rpm, 7000.0, rel_tol=1e-9)


def test_close_ratios_and_a_peaky_engine_shift_before_the_redline():
    # Power falls past 5000, and each upshift only drops ~15% of the revs, so the
    # taller gear catches up while there is still rev range left.
    inputs = calc.Inputs.from_dict({"gears": CLOSE, "torque_curve": PEAKY})
    for cross in calc.shift_crossovers(inputs):
        assert not cross.at_redline
        assert 1000.0 < cross.rpm < 7000.0


def test_wide_ratios_with_a_climbing_engine_shift_at_the_redline():
    # STOCK's 5.14 -> 2.83 first upshift drops the revs by 45%. Landing at 3854
    # rpm makes less power than 7000 rpm does on CURVE, whose power is still
    # rising at the limiter — so every gear is held to the limiter.
    inputs = calc.Inputs.from_dict({"gears": STOCK, "torque_curve": CURVE})
    crossings = calc.shift_crossovers(inputs)
    assert all(c.at_redline for c in crossings)
    assert all(isclose(c.rpm, 7000.0, rel_tol=1e-9) for c in crossings)


def test_crossovers_need_two_gears():
    inputs = calc.Inputs.from_dict({"gears": [3.0], "torque_curve": CURVE})
    assert calc.shift_crossovers(inputs) == []


def test_at_speed_reports_force_only_inside_the_curve():
    data = {"gears": STOCK, "torque_curve": CURVE}
    fast = calc.at_speed(data, 100.0)
    assert fast["force"] is not None and fast["force"] > 0
    # Crawling: 1st gear at 1 km/h is below the curve's first point.
    assert calc.at_speed(data, 1.0)["force"] is None
    assert calc.at_speed({"gears": STOCK}, 100.0)["force"] is None
