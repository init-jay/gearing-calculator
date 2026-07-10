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


def test_at_speed_reports_the_operating_point_only_inside_the_curve():
    data = {"gears": STOCK, "torque_curve": CURVE}
    fast = calc.at_speed(data, 100.0)
    assert all(fast[k] is not None and fast[k] > 0 for k in ("torque", "power", "force"))
    # Crawling: 1st gear at 1 km/h is below the curve's first point. The three
    # curve-dependent fields go None together, so a chart marker can test any one.
    slow = calc.at_speed(data, 1.0)
    assert (slow["torque"], slow["power"], slow["force"]) == (None, None, None)
    bare = calc.at_speed({"gears": STOCK}, 100.0)
    assert (bare["torque"], bare["power"], bare["force"]) == (None, None, None)


def test_at_speed_operating_point_is_self_consistent():
    # The marker on the torque curve and the marker on the power curve must sit at
    # the same rpm and satisfy power = torque * rpm / K, or they describe different
    # engines. Force must agree with the same torque through the same gear.
    data = {"gears": STOCK, "torque_curve": CURVE}
    at = calc.at_speed(data, 100.0)
    inputs = calc.Inputs.from_dict(data)

    assert isclose(at["torque"], calc.torque_at_rpm(CURVE, at["rpm"]), rel_tol=1e-12)
    assert isclose(at["power"], calc.power_at_rpm(at["torque"], at["rpm"]), rel_tol=1e-12)
    assert isclose(at["force"], calc._effort(inputs, at["ratio"], at["rpm"]), rel_tol=1e-12)


def test_at_speed_operating_point_walks_up_the_curve_with_speed():
    # Within one gear, more speed is more rpm, so the markers slide along the
    # curves. Torque may rise or fall; rpm and the marker's x position may not.
    data = {"gears": STOCK, "torque_curve": CURVE}
    inputs = calc.Inputs.from_dict(data)
    top = calc.shift_points(inputs)[0].speed
    rpms = [calc.at_speed(data, top * f)["rpm"] for f in (0.6, 0.7, 0.8, 0.9)]
    assert rpms == sorted(rpms)
    assert all(calc.at_speed(data, top * f)["torque"] is not None for f in (0.6, 0.9))


# ---------------------------- gear spread ---------------------------------


def test_spread_tiles_the_whole_range():
    inputs = calc.Inputs.from_dict({"gears": STOCK})
    spans = calc.gear_spread(inputs)
    assert [s.gear for s in spans] == [1, 2, 3, 4, 5, 6]
    assert spans[0].from_speed == 0.0
    # Each span picks up exactly where the last one stopped.
    for a, b in zip(spans, spans[1:]):
        assert isclose(a.to_speed, b.from_speed, rel_tol=1e-12)
    assert isclose(sum(s.share for s in spans), 1.0, rel_tol=1e-12)


def test_spread_ends_at_the_reported_top_speed():
    data = {"gears": STOCK}
    spans = calc.gear_spread(calc.Inputs.from_dict(data))
    assert isclose(spans[-1].to_speed, calc.compute(data)["max_speed"], rel_tol=1e-12)


def test_spread_boundaries_are_the_shift_speeds():
    inputs = calc.Inputs.from_dict({"gears": STOCK})
    shifts = calc.shift_points(inputs)
    spans = calc.gear_spread(inputs)
    for shift, span in zip(shifts, spans):
        assert isclose(span.to_speed, shift.speed, rel_tol=1e-12)


def test_spread_share_is_the_span_over_the_top_speed():
    inputs = calc.Inputs.from_dict({"gears": STOCK})
    spans = calc.gear_spread(inputs)
    top = spans[-1].to_speed
    for s in spans:
        assert isclose(s.share, (s.to_speed - s.from_speed) / top, rel_tol=1e-12)


def test_a_single_gear_covers_everything():
    spans = calc.gear_spread(calc.Inputs.from_dict({"gears": [3.0]}))
    assert len(spans) == 1
    assert spans[0].from_speed == 0.0
    assert isclose(spans[0].share, 1.0, rel_tol=1e-12)


def test_spread_is_unit_agnostic():
    # The same drivetrain in either unit system divides the range identically:
    # every boundary is a ratio of speeds, and the units cancel.
    metric = calc.gear_spread(calc.Inputs.from_dict({"gears": STOCK, "tire": 635.0}))
    imperial = calc.gear_spread(
        calc.Inputs.from_dict({"gears": STOCK, "tire": 25.0, "units": "imperial"})
    )
    for a, b in zip(metric, imperial):
        assert isclose(a.share, b.share, rel_tol=1e-9)


def test_shifting_early_shrinks_every_gear_but_the_last():
    # Upshifting below the redline hands road speed to the next gear up, so the
    # intermediate gears cover less and the top gear — which still runs to the
    # limiter — picks up the slack.
    late = calc.gear_spread(calc.Inputs.from_dict({"gears": STOCK, "shift_rpm": 7000}))
    early = calc.gear_spread(calc.Inputs.from_dict({"gears": STOCK, "shift_rpm": 5000}))
    assert all(e.share < l.share for e, l in zip(early[:-1], late[:-1]))
    assert early[-1].share > late[-1].share
    assert isclose(sum(s.share for s in early), 1.0, rel_tol=1e-12)


def test_a_taller_top_gear_covers_more_of_the_range():
    short = calc.gear_spread(calc.Inputs.from_dict({"gears": [3.0, 2.0, 1.0]}))
    tall = calc.gear_spread(calc.Inputs.from_dict({"gears": [3.0, 2.0, 0.7]}))
    assert tall[-1].share > short[-1].share


def test_spread_of_a_real_gearbox_matches_a_hand_calc():
    # Boundaries are shift_rpm * (1/ratio), so a gear's share is the gap between
    # consecutive 1/ratio values over the top gear's 1/ratio — the tire, diff and
    # units all cancel. The last gear also gains the run from shift_rpm to redline.
    gears = [3.0, 2.0, 1.0]
    inputs = calc.Inputs.from_dict({"gears": gears, "shift_rpm": 6000, "max_rpm": 6000})
    spans = calc.gear_spread(inputs)
    inv = [1 / g for g in gears]
    top = inv[-1]
    expected = [inv[0] / top, (inv[1] - inv[0]) / top, (inv[2] - inv[1]) / top]
    for span, want in zip(spans, expected):
        assert isclose(span.share, want, rel_tol=1e-12)


def test_out_of_order_gears_never_report_a_negative_share():
    # A gear list entered backwards makes a shift speed exceed the next one.
    spans = calc.gear_spread(calc.Inputs.from_dict({"gears": [1.0, 2.0, 3.0]}))
    assert all(s.share >= 0.0 for s in spans)
    assert all(s.to_speed >= s.from_speed for s in spans)


def test_compute_exposes_the_spread():
    spread = calc.compute({"gears": STOCK})["spread"]
    assert len(spread) == 6
    assert set(spread[0]) == {"gear", "from_speed", "to_speed", "share"}
    assert isclose(sum(s["share"] for s in spread), 1.0, rel_tol=1e-12)


# --------------------------- acceleration ---------------------------------

# One gear, flat torque: the tractive force is the same at every road speed, so
# the run has a closed form and the integrator has nowhere to hide.
FLAT = [(1000, 200.0), (7000, 200.0)]


def _spin(ratio, final_drive=3.64, transfer=1.0):
    """The inertia factor calc applies to the mass in ``a = F/m``."""
    return 1.04 + 0.0025 * calc.overall_ratio(ratio, final_drive, transfer) ** 2


def test_constant_force_matches_the_closed_form_time():
    # Constant force over a constant mass: t = m_eff * dv / F exactly, and a
    # midpoint sum of a constant integrand is exact too, so this is not an
    # approximation — it pins the integrator to the algebra.
    inputs = calc.Inputs.from_dict({
        "gears": [1.0], "torque_curve": FLAT, "weight": 1500.0,
        "mu": 100.0,  # absurd, so friction never caps the engine
        "shift_time": 0.0,
    })
    force = calc.tractive_effort(200.0, 1.0, inputs.final_drive, inputs.transfer, inputs.tire)
    mass = 1500.0 * _spin(1.0)
    expected = 80.0 * calc.MPS_PER_KMH * mass / force

    assert isclose(calc.accel_time(inputs, 80.0), expected, rel_tol=1e-12)


def test_metric_and_imperial_agree_at_a_common_target_speed():
    # The same physical car, described twice. 50 mph is 80.4672 km/h, so the two
    # runs are the same run and must take the same number of seconds.
    common = {"gears": [3.0, 2.0, 1.4, 1.0], "final_drive": 3.9,
              "max_rpm": 7000, "mu": 1.1, "shift_time": 0.25}
    metric = calc.Inputs.from_dict({
        **common, "units": "metric", "tire": 634.3, "weight": 1400.0, "torque_curve": CURVE,
    })
    imperial = calc.Inputs.from_dict({
        **common, "units": "imperial", "tire": 634.3 / calc.MM_PER_INCH,
        "weight": 1400.0 / calc.KG_PER_LB,
        "torque_curve": calc.convert_curve(CURVE, "imperial"),
    })

    assert isclose(calc.accel_time(metric, 80.4672), calc.accel_time(imperial, 50.0), rel_tol=1e-9)


def test_more_weight_is_slower_when_engine_limited():
    # Grip high enough that the engine, not the tires, is always the limit —
    # otherwise mass cancels out of a = mu*m*g / (m*spin) and this proves nothing.
    def run(weight):
        return calc.accel_time(calc.Inputs.from_dict({
            "gears": STOCK, "torque_curve": CURVE, "mu": 100.0,
            "shift_time": 0.0, "weight": weight,
        }), 100.0)

    assert run(2800.0) > run(1400.0)


def test_more_friction_is_quicker_when_traction_limited():
    # The mirror image: a single tall-geared gear with far more torque than the
    # tires can take, so mu is the binding constraint the whole way.
    def run(mu):
        return calc.accel_time(calc.Inputs.from_dict({
            "gears": [3.0], "torque_curve": [(1000, 600.0), (7000, 600.0)],
            "weight": 1400.0, "shift_time": 0.0, "mu": mu,
        }), 40.0)

    assert run(0.4) < run(0.2)


def test_weight_cancels_out_of_a_traction_limited_launch():
    # a = mu*m*g / (m*spin): the mass divides out. Counter-intuitive, and a
    # direct consequence of taking the whole weight as load on the driven axle.
    def run(weight):
        return calc.accel_time(calc.Inputs.from_dict({
            "gears": [3.0], "torque_curve": [(1000, 600.0), (7000, 600.0)],
            "mu": 0.2, "shift_time": 0.0, "weight": weight,
        }), 40.0)

    assert isclose(run(1000.0), run(2000.0), rel_tol=1e-12)


def test_shift_time_adds_exactly_n_times_the_dead_time():
    # Dead time never touches the integrand — nothing decelerates the car while
    # the clutch is out — so it comes off the total as clean linear superposition.
    def inputs(shift_time):
        return calc.Inputs.from_dict({
            "gears": STOCK, "torque_curve": CURVE, "weight": 1400.0,
            "mu": 1.0, "shift_time": shift_time,
        })

    shifts = calc.acceleration(inputs(0.4)).shifts
    assert shifts > 0
    gap = calc.accel_time(inputs(0.4), 100.0) - calc.accel_time(inputs(0.0), 100.0)
    assert isclose(gap, shifts * 0.4, rel_tol=1e-12)


def test_no_torque_curve_has_no_acceleration():
    # Same contract as `effort_curves`: no engine data, no output to give.
    inputs = calc.Inputs(gears=STOCK)
    assert calc.acceleration(inputs) is None
    assert calc.accel_time(inputs, 100.0) is None
    assert calc.compute({"gears": STOCK})["acceleration"] is None


def test_a_car_that_tops_out_below_the_target_never_arrives():
    # An engine that runs out of revs in top gear before 100 km/h. The run exists
    # — there is a curve — but it never finishes, and those are different answers.
    inputs = calc.Inputs.from_dict({
        "gears": [12.0], "final_drive": 5.0, "max_rpm": 3000, "torque_curve": FLAT,
    })
    accel = calc.acceleration(inputs)
    assert accel is not None
    assert accel.time is None
    assert calc.accel_time(inputs, 100.0) is None


def test_acceleration_reports_the_benchmark_target_per_unit_system():
    metric = calc.Inputs.from_dict({"gears": STOCK, "torque_curve": CURVE})
    imperial = calc.Inputs.from_dict({
        "gears": STOCK, "units": "imperial", "tire": 634.3 / calc.MM_PER_INCH,
        "torque_curve": calc.convert_curve(CURVE, "imperial"),
    })
    assert calc.acceleration(metric).target_speed == 100.0
    assert calc.acceleration(imperial).target_speed == 60.0


def test_the_traction_limited_region_is_reported():
    strong = calc.Inputs.from_dict({
        "gears": STOCK, "torque_curve": CURVE, "weight": 1400.0, "mu": 0.3,
    })
    assert calc.acceleration(strong).traction_limited_to > 0.0

    # Grip the tires will never run out of: the engine is the limit from rest.
    sticky = calc.Inputs.from_dict({
        "gears": STOCK, "torque_curve": CURVE, "weight": 1400.0, "mu": 100.0,
    })
    assert calc.acceleration(sticky).traction_limited_to == 0.0
    assert calc.acceleration(sticky).traction_limited_gear == 0


def test_the_traction_limited_gear_is_the_one_making_the_force():
    # The two halves of the sentence have to describe the same point on the chart:
    # the named gear's curve must be at the grip line at the named speed, and that
    # speed must lie in the slice of the run the named gear covers.
    for mu in (0.3, 0.6, 0.9):
        inputs = calc.Inputs.from_dict({
            "gears": STOCK, "torque_curve": CURVE, "weight": 1400.0, "mu": mu,
        })
        accel = calc.acceleration(inputs)
        assert accel.traction_limited_to > 0.0

        gear = accel.traction_limited_gear
        ratio = inputs.gears[gear - 1]
        force = calc._effort(inputs, ratio, calc._rpm(inputs, accel.traction_limited_to, ratio))
        assert force >= calc.traction_limit(inputs) * (1 - 1e-9)

        span = calc.gear_spread(inputs)[gear - 1]
        # Inclusive at the top: the region ends *at* the upshift whenever the gear
        # is still above the limit when it runs out of revs.
        assert span.from_speed <= accel.traction_limited_to <= span.to_speed + 1e-9


def test_the_traction_limited_region_is_not_clipped_by_the_benchmark():
    # A car still spinning its tires at 100 km/h must not report being grip-limited
    # to exactly 100 km/h: that is where the timed run stops, not where grip stops.
    inputs = calc.Inputs.from_dict({
        "gears": STOCK, "torque_curve": CURVE, "weight": 1400.0, "mu": 0.2,
    })
    accel = calc.acceleration(inputs)
    assert accel.traction_limited_to > accel.target_speed
    assert accel.traction_limited_gear > calc.gear_at_speed(inputs, accel.target_speed)


def test_the_traction_limited_region_is_reported_for_a_car_too_slow_to_benchmark():
    # No 0-100 time to speak of, but the tires still give up: the grip-limited
    # region comes from the rev range, so it survives `time` being None.
    inputs = calc.Inputs.from_dict({
        "gears": [5.14], "torque_curve": CURVE, "max_rpm": 3000.0,
        "weight": 1400.0, "mu": 0.3,
    })
    accel = calc.acceleration(inputs)
    assert accel.time is None
    assert accel.traction_limited_to > 0.0
    assert accel.traction_limited_gear == 1


def test_traction_limited_ends_where_the_curve_crosses_the_grip_line():
    # The headline claim: the reported speed is the crossing the chart draws. One
    # gear, so the region can only end by the curve descending through the limit.
    inputs = calc.Inputs.from_dict({
        "gears": [3.0], "torque_curve": [(1000, 400.0), (4000, 400.0), (7000, 200.0)],
        "weight": 1400.0, "mu": 0.9,
    })
    speed, gear = calc.traction_limited(inputs)
    assert gear == 1
    limit = calc.traction_limit(inputs)
    ratio = inputs.gears[0]

    def force(v):
        return calc._effort(inputs, ratio, calc._rpm(inputs, v, ratio))

    # A true crossing: at the limit here, and below it just past here.
    assert isclose(force(speed), limit, rel_tol=1e-6)
    assert force(speed + 0.01) < limit


def test_traction_limited_names_the_same_speed_in_both_unit_systems():
    # Same car, same grip line, same crossing — only the number on the axis differs.
    common = {"gears": STOCK, "max_rpm": 7000.0, "mu": 0.3}
    metric = calc.Inputs.from_dict({**common, "weight": 1400.0, "torque_curve": CURVE})
    imperial = calc.Inputs.from_dict({
        **common, "units": "imperial",
        "weight": calc.convert_weight(1400.0, "imperial"),
        "tire": calc.tire_diameter(*calc.DEFAULT_TIRE_SIZE, units="imperial"),
        "torque_curve": calc.convert_curve(calc.normalize_curve(CURVE), "imperial"),
    })
    kmh, metric_gear = calc.traction_limited(metric)
    mph, imperial_gear = calc.traction_limited(imperial)
    assert metric_gear == imperial_gear
    assert isclose(kmh * calc.MPS_PER_KMH, mph * calc.MPS_PER_MPH, rel_tol=1e-6)


def test_less_friction_stays_traction_limited_into_a_higher_gear():
    # Halving mu cannot shorten the grip-limited part of the run.
    def limited(mu):
        accel = calc.acceleration(calc.Inputs.from_dict({
            "gears": STOCK, "torque_curve": CURVE, "weight": 1400.0, "mu": mu,
        }))
        return accel.traction_limited_to, accel.traction_limited_gear

    slippery_speed, slippery_gear = limited(0.3)
    grippy_speed, grippy_gear = limited(0.9)
    assert slippery_speed > grippy_speed
    assert slippery_gear >= grippy_gear


def test_acceleration_rejects_impossible_vehicles():
    for bad in ({"weight": 0.0}, {"weight": -1.0}, {"mu": 0.0}, {"mu": -0.5},
                {"shift_time": -0.1}):
        with pytest.raises(ValueError):
            calc.acceleration(calc.Inputs.from_dict({
                "gears": STOCK, "torque_curve": CURVE, **bad,
            }))


def test_accel_time_rejects_a_nonpositive_target():
    inputs = calc.Inputs.from_dict({"gears": STOCK, "torque_curve": CURVE})
    for target in (0.0, -10.0):
        with pytest.raises(ValueError):
            calc.accel_time(inputs, target)
    with pytest.raises(ValueError):
        calc.accel_time(inputs, 100.0, steps=0)


def test_from_dict_defaults_match_the_dataclass():
    # The browser omits a field only when it means "use the default", so the two
    # sets of defaults have to agree or the form and the model quietly diverge.
    bare = calc.Inputs(gears=STOCK)
    built = calc.Inputs.from_dict({"gears": STOCK})
    assert (built.weight, built.mu, built.shift_time) == (bare.weight, bare.mu, bare.shift_time)


def test_compute_exposes_the_acceleration():
    accel = calc.compute({"gears": STOCK, "torque_curve": CURVE})["acceleration"]
    assert set(accel) == {"target_speed", "time", "shifts", "traction_limited_to",
                          "traction_limited_gear"}
    assert accel["time"] > 0.0


def test_convert_weight_round_trips_through_both_unit_systems():
    # 1 lb is 0.45359237 kg by definition, and the constant is derived from
    # NM_PER_LBFT rather than pasted, so this checks the derivation too.
    assert isclose(calc.convert_weight(1.0, "metric"), 0.45359237, rel_tol=1e-12)
    assert isclose(calc.convert_weight(calc.convert_weight(1410.0, "imperial"), "metric"),
                   1410.0, rel_tol=1e-12)
    with pytest.raises(ValueError):
        calc.convert_weight(1.0, "furlongs")


def test_traction_limit_is_the_force_the_accel_run_caps_at():
    # The chart draws this line and the integrator caps at it. If they were
    # computed separately they could drift; a car pinned to the limit for the
    # whole run must accelerate at exactly mu*g / spin.
    inputs = calc.Inputs.from_dict({
        "gears": [3.0], "torque_curve": [(1000, 600.0), (7000, 600.0)],
        "weight": 1400.0, "mu": 0.2, "shift_time": 0.0,
    })
    limit = calc.traction_limit(inputs)
    assert isclose(limit, 0.2 * 1400.0 * calc.STANDARD_GRAVITY, rel_tol=1e-12)

    # Force is grip-capped everywhere, so a = limit / (m * spin) is constant.
    accel = limit / (1400.0 * _spin(3.0))
    expected = 40.0 * calc.MPS_PER_KMH / accel
    assert isclose(calc.accel_time(inputs, 40.0), expected, rel_tol=1e-12)


def test_traction_limit_is_coulomb_friction_on_the_normal_force():
    # F_max = mu * N, with N the weight on the tires. The two factors are separable
    # because only one of them is a tire property: a load model replaces N alone.
    inputs = calc.Inputs.from_dict({"gears": [3.0], "weight": 1400.0, "mu": 1.1})
    assert isclose(calc.normal_force(inputs), 1400.0 * calc.STANDARD_GRAVITY, rel_tol=1e-12)
    assert isclose(calc.traction_limit(inputs),
                   1.1 * calc.normal_force(inputs), rel_tol=1e-12)

    # mu is a pure ratio: doubling it doubles the force without touching the load.
    doubled = calc.Inputs.from_dict({"gears": [3.0], "weight": 1400.0, "mu": 2.2})
    assert calc.normal_force(doubled) == calc.normal_force(inputs)
    assert isclose(calc.traction_limit(doubled), 2.0 * calc.traction_limit(inputs), rel_tol=1e-12)

    with pytest.raises(ValueError):
        calc.traction_limit(calc.Inputs.from_dict({"gears": [3.0], "mu": 0.0}))
    with pytest.raises(ValueError):
        calc.normal_force(calc.Inputs.from_dict({"gears": [3.0], "weight": -1.0}))


def test_normal_force_is_unit_system_consistent():
    # A pound-mass weighs a pound-force under standard gravity, so imperial's
    # normal force is the weight figure unchanged — no g anywhere.
    metric = calc.Inputs.from_dict({"weight": 1400.0, "gears": [3.0]})
    imperial = calc.Inputs.from_dict({
        "gears": [3.0], "units": "imperial", "weight": 1400.0 / calc.KG_PER_LB,
    })
    assert calc.normal_force(imperial) == imperial.weight
    assert isclose(calc.normal_force(metric),
                   calc.normal_force(imperial) * calc.N_PER_LBF, rel_tol=1e-9)


def test_traction_limit_is_unit_system_consistent():
    # A pound-force is a pound-mass under gravity, so imperial needs no g.
    metric = calc.Inputs.from_dict({"gears": [3.0], "weight": 1400.0, "mu": 1.1})
    imperial = calc.Inputs.from_dict({
        "gears": [3.0], "units": "imperial", "weight": 1400.0 / calc.KG_PER_LB, "mu": 1.1,
    })
    # Same physical force, stated in newtons and in pounds-force.
    assert isclose(calc.traction_limit(metric),
                   calc.traction_limit(imperial) * calc.N_PER_LBF, rel_tol=1e-9)


def test_traction_limit_scales_with_mu_and_weight():
    def limit(weight, mu):
        return calc.traction_limit(calc.Inputs.from_dict({
            "gears": [3.0], "weight": weight, "mu": mu,
        }))

    assert isclose(limit(2800.0, 1.0), 2.0 * limit(1400.0, 1.0), rel_tol=1e-12)
    assert isclose(limit(1400.0, 2.0), 2.0 * limit(1400.0, 1.0), rel_tol=1e-12)


def test_compute_exposes_the_traction_limit():
    result = calc.compute({"gears": STOCK, "torque_curve": CURVE, "weight": 1400.0, "mu": 1.0})
    assert isclose(result["traction_limit"], 1400.0 * calc.STANDARD_GRAVITY, rel_tol=1e-12)
    # Same units as the curves it is drawn across.
    assert result["force_unit"] == "N"


def test_gears_at_speeds_matches_at_speed_while_following_the_schedule():
    # Accelerating (or with no torque curve to hold against), the batch
    # entrypoint is at_speed's gear resolution over a list; disagreeing there
    # would make the lap gear map contradict the gauges.
    data = {"gears": STOCK}
    speeds = [0.0, 15.0, 42.5, 60.0, 88.8, 130.0, 220.0, 500.0]
    batch = calc.gears_at_speeds(data, speeds)
    assert batch == [calc.at_speed(data, v)["gear"] for v in speeds]


def _hold_scenario():
    """Speeds bracketing the downshift-hysteresis decision in 4th gear.

    Returns ``(data, v_into_4th, v_cross, v_hold)``: ``v_cross`` is the 3->4
    tractive-effort crossover — below it 3rd pulls harder than 4th, so the
    downshift pays — and ``v_hold`` is midway between there and the 3->4
    shift: scheduled for 3rd, but with 4th still the stronger gear, so
    nothing to gain by taking the downshift. (``v_cross`` itself is a
    bisected root, a float knife-edge, so assertions use speeds clearly
    inside each regime.)

    Uses PEAKY: under CURVE every STOCK pair holds to the redline (the lower
    gear never stops pulling harder), which leaves no band to hold 4th in.
    """
    data = {"gears": STOCK, "torque_curve": PEAKY}
    inputs = calc.Inputs.from_dict(data)
    shifts = calc.shift_points(inputs)
    v_into_4th = shifts[2].speed
    v_cross = calc.shift_crossovers(inputs)[2].speed
    # The scenario only bites if the crossover is inside 3rd's scheduled band.
    assert v_cross < shifts[2].speed
    return data, v_into_4th, v_cross, (v_cross + v_into_4th) / 2


def test_gears_at_speeds_holds_the_gear_while_it_pulls_harder():
    data, v_into_4th, _, v_hold = _hold_scenario()
    # Lifting from the 3->4 shift to a speed still above the effort
    # crossover, 4th is held — even though the schedule (what at_speed
    # reports) prescribes 3rd there.
    assert calc.gears_at_speeds(data, [v_into_4th, v_hold]) == [4, 4]
    assert calc.at_speed(data, v_hold)["gear"] == 3


def test_gears_at_speeds_downshifts_where_the_lower_gear_wins():
    data, v_into_4th, v_cross, _ = _hold_scenario()
    # Below the crossover 3rd out-pulls 4th, so the downshift happens — and
    # goes no further, 3rd being the strongest legal gear at that speed.
    assert calc.gears_at_speeds(data, [v_into_4th, v_cross * 0.99]) == [4, 3]


def test_gears_at_speeds_never_downshifts_into_first():
    data, v_into_4th, _, _ = _hold_scenario()
    # Braking to walking pace steps down through the crossovers but stops at
    # 2nd — 1st is a launch gear, not a corner gear.
    assert calc.gears_at_speeds(data, [v_into_4th, 5.0]) == [4, 2]
    # 1st still appears where the schedule itself starts a sample there.
    assert calc.gears_at_speeds(data, [5.0, v_into_4th, 5.0]) == [1, 4, 2]
    # The driver rule outlives the torque curve: schedule-only decel too.
    del data["torque_curve"]
    assert calc.gears_at_speeds(data, [v_into_4th, 5.0]) == [4, 2]


def test_gears_at_speeds_without_a_curve_downshifts_on_schedule():
    # No torque curve -> no peak to hold against -> schedule both ways.
    data, v_into_4th, _, v_hold = _hold_scenario()
    del data["torque_curve"]
    assert calc.gears_at_speeds(data, [v_into_4th, v_hold]) == [4, 3]


def test_gears_at_speeds_reaccelerates_out_of_a_held_gear():
    data, v_into_4th, _, v_hold = _hold_scenario()
    # Corner exit: the held 4th stays held as speed comes back up, and the
    # schedule takes over again once it re-enters 4th's own band.
    got = calc.gears_at_speeds(data, [v_into_4th, v_hold, v_into_4th, v_into_4th * 1.5])
    assert got == [4, 4, 4, 5]


def test_gears_at_speeds_boundary_and_clamp():
    data = {"gears": STOCK}
    shifts = calc.shift_points(calc.Inputs.from_dict(data))
    # At exactly a shift speed the shift has just happened -> the next gear.
    assert calc.gears_at_speeds(data, [shifts[0].speed]) == [shifts[0].to_gear]
    # Beyond the last shift the answer clamps to the top gear.
    assert calc.gears_at_speeds(data, [shifts[-1].speed * 10]) == [len(STOCK)]


def test_gears_at_speeds_is_monotonic_in_speed():
    data = {"gears": STOCK}
    speeds = [v * 2.5 for v in range(120)]
    gears = calc.gears_at_speeds(data, speeds)
    assert gears == sorted(gears)
    assert set(gears) == set(range(1, len(STOCK) + 1))
