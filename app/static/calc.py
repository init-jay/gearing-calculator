"""Gearing calculator core math.

Pure standard-library Python so the exact same module runs unmodified under
Pyodide in the browser *and* is importable by pytest on the server. There is no
FastAPI / web dependency here — just the drivetrain math.

The model relates engine RPM to road speed through the drivetrain::

    overall_ratio = gear_ratio * final_drive * transfer_case
    speed = rpm * tire_circumference / overall_ratio * (1 - slip)  (per minute)

with unit conversions folded in for km/h (tire diameter in mm, the default) and
mph (tire diameter in inches).

Given an engine torque curve it also computes *tractive effort* — the force the
tires can push the car with, in each gear, at each road speed::

    axle_torque    = engine_torque * overall_ratio
    tractive_force = axle_torque / rolling_radius

Plotting that per gear shows why peak power, not peak torque, is what you chase:
the speed at which one gear's curve drops below the next gear's is the optimal
upshift, and it is rarely the redline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import pi

MM_PER_INCH = 25.4
INCHES_PER_FOOT = 12.0
METERS_PER_FOOT = 0.3048
NM_PER_LBFT = 1.3558179483314004

# Standard gravity, m/s^2: turns a mass into the weight pressing the tires down.
STANDARD_GRAVITY = 9.80665

# A pound-force is a pound-foot of torque on a one-foot arm, so the force
# conversion falls out of the torque one: 1.3558... / 0.3048 = 4.4482216...
N_PER_LBF = NM_PER_LBFT / METERS_PER_FOOT
# ...and a pound-force is *by definition* a pound-mass under standard gravity, so
# dividing g back out gives lb -> kg = 0.45359237 exactly. Deriving both from
# NM_PER_LBFT keeps every mass and force conversion tied to one root constant.
KG_PER_LB = N_PER_LBF / STANDARD_GRAVITY

MPS_PER_KMH = 1000.0 / 3600.0
# Reuses the 63360 inches per mile already folded into MPH_CONST: 63360 * 25.4 mm
# is 1609.344 m, over 3600 s, so a mph is 0.44704 m/s.
MPS_PER_MPH = 63360.0 * MM_PER_INCH / 1000.0 / 3600.0

# Imperial constant: mph = rpm * tire_dia_in / (overall_ratio * MPH_CONST).
# Derivation: inches/hour = rpm * pi * dia * 60; miles/hour divides by 63360,
# so MPH_CONST = 63360 / (pi * 60) = 336.135...
MPH_CONST = 63360.0 / (pi * 60.0)

# hp = torque_lbft * rpm / 5252.11...  One horsepower is 33000 ft-lb per minute,
# and one revolution carries the torque through 2*pi radians.
HP_CONST = 33000.0 / (2.0 * pi)
# kW = torque_nm * rpm / 9549.29...  Watts are N.m * rad/s; rpm -> rad/s is
# 2*pi/60, and dividing by 1000 gives kW, so the constant is 30000 / pi.
KW_CONST = 30000.0 / pi

# The standing-start yardstick, per market. These are *different* physical speeds
# — 100 km/h is 62.14 mph — but each is the benchmark its readers know, so the
# quoted time follows the selected unit system rather than converting.
BENCHMARK_KMH = 100.0
BENCHMARK_MPH = 60.0

# Midpoint samples across the speed range. Fixed, like the scan and bisection
# counts in `shift_crossovers`, so a run is deterministic and unit-independent.
ACCEL_STEPS = 2000

# Lateral acceleration (G) above which `gears_at_speeds` treats the car as
# loaded up mid-corner and defers downshifts. Road tires hit their cornering
# limit around 0.9-1.1 G; 0.4 G is comfortably past "changing lanes" while
# catching every real corner well before the apex.
CORNER_LATERAL_G = 0.4

Units = str  # "imperial" | "metric"

# (rpm, torque) points. Torque is N.m under metric, lb-ft under imperial.
TorqueCurve = list  # list[tuple[float, float]]


def _validate_units(units: Units) -> None:
    if units not in ("imperial", "metric"):
        raise ValueError(f"units must be 'imperial' or 'metric', got {units!r}")


def tire_diameter(
    section_width: float,
    aspect_ratio: float,
    wheel_diameter: float,
    units: Units = "metric",
) -> float:
    """Overall tire diameter from a standard tire size, e.g. 225/45R17.

    ``section_width`` is the tread width in mm, ``aspect_ratio`` is the sidewall
    height as a percentage of that width, and ``wheel_diameter`` is the rim
    diameter in inches — the three numbers as printed on the sidewall, in the
    units they are always printed in, regardless of ``units``.

    ``units`` selects the *return* unit: mm for metric, inches for imperial.
    The sidewall is counted twice, once above the rim and once below.
    """
    _validate_units(units)
    if section_width <= 0 or aspect_ratio <= 0 or wheel_diameter <= 0:
        raise ValueError("tire dimensions must be positive")
    mm = wheel_diameter * MM_PER_INCH + 2.0 * section_width * aspect_ratio / 100.0
    return mm if units == "metric" else mm / MM_PER_INCH


# Stock tire, used for the default inputs below and by the frontend's form.
DEFAULT_TIRE_SIZE = (225.0, 45.0, 17.0)
DEFAULT_TIRE_MM = tire_diameter(*DEFAULT_TIRE_SIZE)  # 634.3 mm


def overall_ratio(gear_ratio: float, final_drive: float, transfer: float) -> float:
    """Total drivetrain reduction from crankshaft to wheel."""
    return gear_ratio * final_drive * transfer


def speed_at_rpm(
    rpm: float,
    gear_ratio: float,
    final_drive: float,
    transfer: float,
    tire: float,
    slip: float = 0.0,
    units: Units = "metric",
) -> float:
    """Road speed at a given engine ``rpm`` in the selected gear.

    ``tire`` is the tire *diameter* — millimetres for metric (returns km/h), or
    inches for imperial (returns mph). ``slip`` is torque-converter slip as
    a fraction in ``[0, 1)`` and reduces effective speed.
    """
    _validate_units(units)
    ratio = overall_ratio(gear_ratio, final_drive, transfer)
    if ratio <= 0:
        raise ValueError("overall ratio must be positive")
    eff = 1.0 - slip
    if units == "imperial":
        return rpm * tire / (ratio * MPH_CONST) * eff
    # metric: tire diameter in mm -> circumference mm -> km/h
    circ_mm = pi * tire
    return rpm * circ_mm / ratio * eff * 60.0 / 1_000_000.0


def rpm_at_speed(
    speed: float,
    gear_ratio: float,
    final_drive: float,
    transfer: float,
    tire: float,
    slip: float = 0.0,
    units: Units = "metric",
) -> float:
    """Inverse of :func:`speed_at_rpm` — engine rpm needed to hold ``speed``."""
    _validate_units(units)
    ratio = overall_ratio(gear_ratio, final_drive, transfer)
    eff = 1.0 - slip
    if eff <= 0:
        raise ValueError("slip must be < 1")
    if units == "imperial":
        return speed * ratio * MPH_CONST / (tire * eff)
    circ_mm = pi * tire
    return speed * ratio * 1_000_000.0 / (circ_mm * eff * 60.0)


def _rolling_radius(tire: float, units: Units) -> float:
    """Half the tire diameter, in metres (metric) or feet (imperial).

    Those are the units that make ``axle_torque / radius`` come out in newtons
    and pounds-force respectively, given torque in N.m and lb-ft.
    """
    if tire <= 0:
        raise ValueError("tire diameter must be positive")
    if units == "imperial":
        return tire / 2.0 / INCHES_PER_FOOT
    return tire / 2.0 / 1000.0


def power_at_rpm(torque: float, rpm: float, units: Units = "metric") -> float:
    """Engine power from torque and speed: kW for metric, hp for imperial.

    Power is torque times angular velocity, so this is not independent data — a
    torque curve *is* a power curve. That identity is the whole point of the
    tractive-effort plot, so power is always derived here and never taken as an
    input.
    """
    _validate_units(units)
    return torque * rpm / (KW_CONST if units == "metric" else HP_CONST)


def tractive_effort(
    torque: float,
    gear_ratio: float,
    final_drive: float,
    transfer: float,
    tire: float,
    units: Units = "metric",
) -> float:
    """Force at the contact patch: axle torque over the rolling radius.

    Newtons for metric (``torque`` in N.m, ``tire`` in mm), pounds-force for
    imperial (lb-ft, inches).

    ``slip`` is deliberately absent. A slipping torque converter multiplies
    torque rather than losing it, and modelling that needs a converter's own
    K-factor and torque-ratio curves, which this calculator does not ask for.
    Slip still lowers the road *speed* each force is plotted at, via
    :func:`speed_at_rpm`, so the modelled power at the wheels drops by the slip
    fraction — the energy a real converter turns into heat.
    """
    _validate_units(units)
    ratio = overall_ratio(gear_ratio, final_drive, transfer)
    if ratio <= 0:
        raise ValueError("overall ratio must be positive")
    return torque * ratio / _rolling_radius(tire, units)


def normalize_curve(points) -> TorqueCurve:
    """Sort ``(rpm, torque)`` pairs by rpm, dropping non-positive and duplicate ones.

    A later duplicate wins, so an edited row replaces the one it shadows rather
    than creating a vertical segment that :func:`torque_at_rpm` cannot invert.
    """
    clean: dict[float, float] = {}
    for rpm, torque in points:
        rpm, torque = float(rpm), float(torque)
        if rpm > 0 and torque > 0:
            clean[rpm] = torque
    return sorted(clean.items())


def convert_curve(points, to_units: Units) -> TorqueCurve:
    """Restate a torque curve in the other unit system. RPM is unit-agnostic."""
    _validate_units(to_units)
    factor = NM_PER_LBFT if to_units == "metric" else 1.0 / NM_PER_LBFT
    return [(rpm, torque * factor) for rpm, torque in points]


def convert_weight(value: float, to_units: Units) -> float:
    """Restate a vehicle weight in the other unit system: kg under metric, lb under imperial.

    The counterpart of :func:`convert_curve`, and for the same reason — the number
    on screen *is* the data, with no unit-agnostic source to re-derive it from, so
    a unit change has to restate it in place.
    """
    _validate_units(to_units)
    return value * (KG_PER_LB if to_units == "metric" else 1.0 / KG_PER_LB)


def torque_at_rpm(curve: TorqueCurve, rpm: float) -> float:
    """Linear interpolation between the curve's points.

    Outside the curve's range the nearest endpoint is held. Callers sample only
    inside it (see :meth:`Inputs.curve_span`), so no extrapolated torque ever
    reaches a plot; the clamp is there so a lone stray lookup cannot explode.
    """
    if not curve:
        raise ValueError("torque curve is empty")
    if rpm <= curve[0][0]:
        return curve[0][1]
    if rpm >= curve[-1][0]:
        return curve[-1][1]
    for (r0, t0), (r1, t1) in zip(curve, curve[1:]):
        if r0 <= rpm <= r1:
            return t0 + (t1 - t0) * (rpm - r0) / (r1 - r0)
    raise AssertionError("rpm is inside the curve but matched no segment")


@dataclass
class GearCurve:
    """Speed-vs-RPM samples for a single gear."""

    gear: int
    ratio: float
    top_speed: float
    samples: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class ShiftPoint:
    """A single upshift: where the engine lands in the next gear."""

    from_gear: int
    to_gear: int
    speed: float  # road speed at the shift, unchanged by it
    rpm_after: float  # engine rpm once the next gear is engaged
    rpm_drop: float  # how far the needle falls


@dataclass
class GearSpan:
    """The slice of the 0-to-top-speed range one gear covers."""

    gear: int
    from_speed: float
    to_speed: float
    share: float  # fraction of the whole range, so the shares sum to 1


@dataclass
class EffortCurve:
    """Tractive-force-vs-speed samples for a single gear."""

    gear: int
    ratio: float
    samples: list[tuple[float, float]] = field(default_factory=list)  # (speed, force)


@dataclass
class Crossover:
    """Where the next gear starts pulling harder than the current one.

    This is the shift point that maximises acceleration, and it falls at the
    redline only when the engine is still gaining force right up to it.
    """

    from_gear: int
    to_gear: int
    speed: float
    rpm: float  # engine rpm in ``from_gear`` at the crossover
    rpm_after: float  # where it lands in ``to_gear``
    force: float
    at_redline: bool  # no crossing below redline: hold the gear to the limiter


@dataclass
class Accel:
    """A standing-start run to the benchmark speed.

    ``time`` is ``None`` when the car tops out below ``target_speed``. That is a
    different answer from :attr:`Result.acceleration` being ``None``, which means
    there was no torque curve to run at all — "too slow to get there" and "no
    engine data" must not collapse into the same value.

    The two traction fields describe the whole rev range, not just the run to
    ``target_speed``, and can therefore name a speed above it.
    """

    target_speed: float  # the benchmark, in display units
    time: float | None  # seconds, or None when the target is never reached
    shifts: int  # upshifts below the target; time - shifts * shift_time is engine-only
    # The fastest grip still caps the force, 0.0 if never. Not a claim that every
    # slower speed is capped: from rest a soft engine may not break traction at all.
    traction_limited_to: float
    traction_limited_gear: int  # the gear it was still grip-capped in, 0 if never


@dataclass
class Inputs:
    """All calculator inputs; keeps the JS<->Python boundary explicit."""

    gears: list[float]
    final_drive: float = 3.64
    transfer: float = 1.0
    tire: float = DEFAULT_TIRE_MM
    slip: float = 0.0
    max_rpm: float = 7000.0
    units: Units = "metric"
    shift_rpm: float | None = None  # None => shift at the redline
    weight: float = 1400.0  # kg under metric, lb under imperial
    mu: float = 1.0  # coefficient of static friction, rubber on road; a pure ratio
    shift_time: float = 0.3  # seconds of dead time per upshift
    # Empty => no engine data, and every tractive-effort output comes back empty.
    torque_curve: TorqueCurve = field(default_factory=list)

    def effective_shift_rpm(self) -> float:
        """The shift RPM actually used: defaults to, and never exceeds, redline."""
        if self.shift_rpm is None:
            return self.max_rpm
        if self.shift_rpm <= 0:
            raise ValueError("shift_rpm must be positive")
        return min(self.shift_rpm, self.max_rpm)

    def curve_span(self) -> tuple[float, float] | None:
        """The RPM range over which tractive effort is defined, or ``None``.

        Bounded below by the curve's first point and above by the redline, so the
        plot neither extrapolates past the data nor runs past the rev limiter.
        """
        if len(self.torque_curve) < 2:
            return None
        low = self.torque_curve[0][0]
        high = min(self.torque_curve[-1][0], self.max_rpm)
        return (low, high) if high > low else None

    @classmethod
    def from_dict(cls, data: dict) -> "Inputs":
        """Build from a plain dict (e.g. converted from a JS object)."""
        shift_rpm = data.get("shift_rpm")
        return cls(
            gears=[float(g) for g in data["gears"] if float(g) > 0],
            final_drive=float(data.get("final_drive", 3.64)),
            transfer=float(data.get("transfer", 1.0)),
            tire=float(data.get("tire", DEFAULT_TIRE_MM)),
            slip=float(data.get("slip", 0.0)),
            max_rpm=float(data.get("max_rpm", 7000.0)),
            units=data.get("units", "metric"),
            shift_rpm=None if shift_rpm is None else float(shift_rpm),
            weight=float(data.get("weight", 1400.0)),
            mu=float(data.get("mu", 1.0)),
            shift_time=float(data.get("shift_time", 0.3)),
            torque_curve=normalize_curve(data.get("torque_curve") or []),
        )


def _speed(inputs: Inputs, rpm: float, ratio: float) -> float:
    """``speed_at_rpm`` with the drivetrain arguments bound to ``inputs``."""
    return speed_at_rpm(
        rpm, ratio, inputs.final_drive, inputs.transfer, inputs.tire, inputs.slip, inputs.units
    )


def _rpm(inputs: Inputs, speed: float, ratio: float) -> float:
    """``rpm_at_speed`` with the drivetrain arguments bound to ``inputs``."""
    return rpm_at_speed(
        speed, ratio, inputs.final_drive, inputs.transfer, inputs.tire, inputs.slip, inputs.units
    )


def shift_points(inputs: Inputs) -> list[ShiftPoint]:
    """Where the engine lands after each upshift, for ``n - 1`` gear pairs.

    Road speed is continuous across a shift — the clutch reconnects the same
    wheels at the same speed — so the engine must drop to whatever RPM the next
    gear needs to hold that speed::

        rpm_after = shift_rpm * ratio_next / ratio_current

    Tire, final drive, transfer and slip cancel out of that identity, so they are
    deliberately absent here rather than being divided back out through
    :func:`rpm_at_speed`. Routing through the tire diameter would make the result
    depend on the unit system in the last bits of the float, which is enough to
    swing a rounded RPM display by one. ``test_speed_is_continuous_across_the_shift``
    ties this back to :func:`speed_at_rpm` so the two cannot drift apart.

    Gears are used in the order given. A gear list that is not ordered from
    lowest to highest yields a negative ``rpm_drop`` — a downshift — which is
    reported rather than hidden, since it means the ratios are entered wrong.
    """
    shift_rpm = inputs.effective_shift_rpm()
    points: list[ShiftPoint] = []
    for i in range(len(inputs.gears) - 1):
        current, following = inputs.gears[i], inputs.gears[i + 1]
        rpm_after = shift_rpm * following / current
        points.append(
            ShiftPoint(
                from_gear=i + 1,
                to_gear=i + 2,
                speed=_speed(inputs, shift_rpm, current),
                rpm_after=rpm_after,
                rpm_drop=shift_rpm - rpm_after,
            )
        )
    return points


def gear_at_speed(inputs: Inputs, speed: float) -> int:
    """The 1-based gear the car is in at ``speed``, following the shift schedule.

    You are in the first gear whose upshift has not happened yet. At exactly a
    shift speed the shift has *just* happened, so the next gear is the answer —
    which is what puts the engine at that shift's ``rpm_after`` rather than at
    ``shift_rpm``.
    """
    if not inputs.gears:
        raise ValueError("need at least one gear")
    for shift in shift_points(inputs):
        if speed < shift.speed:
            return shift.from_gear
    return len(inputs.gears)


def shift_trace(inputs: Inputs) -> list[tuple[float, float]]:
    """The ``(rpm, speed)`` path of a full acceleration run through every gear.

    ``speed_at_rpm`` is linear in ``rpm``, so each gear contributes a straight
    line and needs only its endpoints. Each shift adds a second point at the
    same speed but lower rpm, which draws the horizontal jump between curves.

    Built from :func:`shift_points` so the trace and the shift table can never
    disagree about where a shift lands.
    """
    if not inputs.gears:
        return []
    shift_rpm = inputs.effective_shift_rpm()
    shifts = shift_points(inputs)
    last = len(inputs.gears) - 1

    trace: list[tuple[float, float]] = [(0.0, 0.0)]
    for i, ratio in enumerate(inputs.gears):
        if i == last:
            # The top gear has nothing to shift into, so it runs out to redline.
            trace.append((inputs.max_rpm, _speed(inputs, inputs.max_rpm, ratio)))
        else:
            shift = shifts[i]
            trace.append((shift_rpm, shift.speed))
            trace.append((shift.rpm_after, shift.speed))
    return trace


def gear_spread(inputs: Inputs) -> list[GearSpan]:
    """How the 0-to-top-speed range is divided between the gears.

    A gear is in use from the upshift that engaged it to the upshift that leaves
    it, so the spans are exactly the gaps between the shift speeds, with 0 at one
    end and the top gear's top speed at the other. They tile the range without
    overlap, so ``share`` sums to 1.

    This says nothing about acceleration and everything about *coverage*: a first
    gear that spans 15% of the range is doing a lot of the work of getting the car
    to speed, and a tall sixth that spans 40% will feel lazy in the middle of it.

    Boundaries follow the shift RPM, but the last span runs out to the redline —
    the top gear has nothing to shift into, exactly as in :func:`shift_trace`.
    Ratios entered out of order can make a shift speed exceed the next one; such a
    gear is reported as covering nothing rather than a negative share.
    """
    if not inputs.gears:
        return []
    top = max(_speed(inputs, inputs.max_rpm, ratio) for ratio in inputs.gears)
    if top <= 0:
        return []

    edges = [0.0] + [s.speed for s in shift_points(inputs)] + [top]
    spans: list[GearSpan] = []
    for i in range(len(inputs.gears)):
        low = min(edges[i], top)
        high = min(max(edges[i + 1], low), top)
        spans.append(GearSpan(i + 1, low, high, (high - low) / top))
    return spans


def _effort(inputs: Inputs, ratio: float, rpm: float) -> float:
    """Tractive force in one gear at one engine speed, bound to ``inputs``."""
    return tractive_effort(
        torque_at_rpm(inputs.torque_curve, rpm),
        ratio, inputs.final_drive, inputs.transfer, inputs.tire, inputs.units,
    )


def engine_samples(inputs: Inputs, step: float = 50.0) -> list[tuple[float, float, float]]:
    """``(rpm, torque, power)`` across the usable rev range.

    Sampled far finer than the entered points because power is quadratic in rpm
    within each straight torque segment, so a coarse grid would draw the power
    curve as a visibly wrong polyline.
    """
    span = inputs.curve_span()
    if span is None:
        return []
    if step <= 0:
        raise ValueError("step must be positive")

    low, high = span
    points: list[float] = []
    rpm = low
    while rpm < high:
        points.append(rpm)
        rpm += step
    points.append(high)

    out: list[tuple[float, float, float]] = []
    for p in points:
        torque = torque_at_rpm(inputs.torque_curve, p)
        out.append((p, torque, power_at_rpm(torque, p, inputs.units)))
    return out


def effort_curves(inputs: Inputs, step: float = 50.0) -> list[EffortCurve]:
    """Tractive force against road speed, one curve per gear.

    Shares :func:`engine_samples`' rpm grid, so every gear's curve is the same
    torque data mapped through a different ratio — which is exactly the claim the
    plot is making.
    """
    samples = engine_samples(inputs, step)
    if not samples:
        return []
    return [
        EffortCurve(
            gear=i,
            ratio=ratio,
            samples=[
                (
                    _speed(inputs, rpm, ratio),
                    tractive_effort(
                        torque, ratio, inputs.final_drive, inputs.transfer, inputs.tire, inputs.units
                    ),
                )
                for rpm, torque, _ in samples
            ],
        )
        for i, ratio in enumerate(inputs.gears, start=1)
    ]


def shift_crossovers(inputs: Inputs, scan: int = 240) -> list[Crossover]:
    """The optimal upshift for each gear pair: where the next gear pulls harder.

    Both gears can hold a band of road speeds. Across that overlap, define
    ``d(v) = force_current(v) - force_next(v)``. The current gear starts ahead —
    it multiplies torque more — and the shift point is the first ``v`` where the
    next gear catches up.

    ``d`` can cross zero more than once when the torque curve is bumpy, so the
    overlap is scanned for the *first* sign change and the root is then bisected
    inside that bracket. Taking the last root, or bisecting the whole interval
    blindly, would name a shift point past a speed where the taller gear was
    already winning.

    A pair with ``at_redline`` never crosses: the current gear is still pulling
    harder when it runs out of revs, so hold it to the limiter.
    """
    span = inputs.curve_span()
    if span is None or len(inputs.gears) < 2:
        return []
    low, high = span

    out: list[Crossover] = []
    for i in range(len(inputs.gears) - 1):
        current, following = inputs.gears[i], inputs.gears[i + 1]

        def delta(speed: float, a: float = current, b: float = following) -> float:
            return _effort(inputs, a, _rpm(inputs, speed, a)) - _effort(inputs, b, _rpm(inputs, speed, b))

        def crossover(speed: float, redline: bool, a: float = current, b: float = following) -> Crossover:
            rpm = min(_rpm(inputs, speed, a), high)
            return Crossover(
                from_gear=i + 1,
                to_gear=i + 2,
                speed=speed,
                rpm=rpm,
                rpm_after=rpm * b / a,
                force=_effort(inputs, a, rpm),
                at_redline=redline,
            )

        # The taller gear cannot run below `low`; the shorter cannot run past `high`.
        v_low = _speed(inputs, low, following)
        v_high = _speed(inputs, high, current)
        if v_low >= v_high or delta(v_high) > 0:
            # Either the ratios are too far apart to overlap at all, or the current
            # gear still wins at the limiter. Both mean: shift at the redline.
            out.append(crossover(v_high, True))
            continue

        lo, hi = v_low, v_high
        if delta(v_low) > 0:
            # Bracket the first sign change before bisecting into it.
            prev = v_low
            for k in range(1, scan + 1):
                v = v_low + (v_high - v_low) * k / scan
                if delta(v) <= 0:
                    lo, hi = prev, v
                    break
                prev = v

        for _ in range(60):
            mid = (lo + hi) / 2.0
            if delta(mid) > 0:
                lo = mid
            else:
                hi = mid
        out.append(crossover((lo + hi) / 2.0, False))

    return out


def normal_force(inputs: Inputs) -> float:
    """The load pressing the driven tires into the road, in the selected unit's force terms.

    Newtons under metric (mass in kg, so weigh it), pounds-force under imperial —
    where no conversion is needed, because a pound-mass *is* a pound-force under
    standard gravity.

    The whole vehicle weight is taken as load on the driven axle. The calculator
    asks for neither weight distribution nor centre-of-gravity height, so it can
    model neither the static split nor the rearward transfer that squats a car as
    it launches. This is therefore the optimistic bound, and the single place a
    real load model would replace.
    """
    if inputs.weight <= 0:
        raise ValueError("weight must be positive")
    return inputs.weight * STANDARD_GRAVITY if inputs.units == "metric" else inputs.weight


def traction_limit(inputs: Inputs) -> float:
    """The most force the tires can transmit before they break away: ``mu * N``.

    Coulomb's law of static friction, with ``mu`` the coefficient between rubber
    and road and ``N`` the :func:`normal_force` on the driven tires. The answer is
    in newtons under metric and pounds-force under imperial.

    Constant with road speed — neither ``mu`` nor ``N`` depends on how fast the
    tires are turning — which is why it plots as a horizontal line across the
    tractive-effort chart. Any part of a gear's curve above it is force the engine
    makes and the tires cannot use.

    That ``mu`` exceeds 1 for a performance tire is not an error. Coulomb friction
    is a model of two rigid surfaces sliding; rubber also keys into road texture
    and adheres to it, and those contributions are not proportional to load. The
    coefficient is fitted to what tires actually do, so 1.0 for a road tire and
    1.4 for a slick are ordinary. What the model keeps is the part that matters
    here: the limit is proportional to load, and independent of speed.
    """
    if inputs.mu <= 0:
        raise ValueError("mu must be positive")
    return inputs.mu * normal_force(inputs)


def traction_limited(inputs: Inputs, scan: int = 240) -> tuple[float, int]:
    """The fastest the car is still grip-capped, and the gear it is capped in.

    ``(0.0, 0)`` when the engine never out-muscles the tires, or when there is no
    torque curve to ask.

    Read straight off the tractive-effort chart: walk the gears the shift schedule
    actually uses, in the order it uses them, and find the last speed at which the
    gear the car is in makes more force than :func:`traction_limit` allows. Each
    gear is searched only across its own slice of the run — the speeds between the
    upshift that engages it and the one that leaves it — because a gear's curve
    says nothing about speeds the car never sees it at.

    Deliberately *not* computed inside :func:`_accel_run`. That loop stops at the
    benchmark speed, so a car still spinning its tires at 100 km/h would report
    being grip-limited to exactly 100 km/h, in whatever gear it happened to be in
    — an artifact of the finish line, not a fact about the car.

    Two different things end the grip-limited region, and both are reported as the
    speed where it ends. Either the gear's curve descends through the limit, which
    is bisected out of the scan grid; or the curve is still above the limit when
    the gear runs out and the upshift drops the force below it, in which case the
    shift speed itself is the answer. The chart shows the second as a trace ending
    above the line rather than crossing it.
    """
    span = inputs.curve_span()
    if span is None or not inputs.gears:
        return 0.0, 0
    if inputs.weight <= 0:
        raise ValueError("weight must be positive")
    if inputs.mu <= 0:
        raise ValueError("mu must be positive")
    if scan <= 0:
        raise ValueError("scan must be positive")

    limit = traction_limit(inputs)
    _, span_high = span

    def force(speed: float, ratio: float) -> float:
        return _effort(inputs, ratio, _rpm(inputs, speed, ratio))

    # The gaps between the shift speeds, exactly as in `gear_spread`: gear i runs
    # from the upshift that engaged it to the one that leaves it, and the top gear
    # runs out to its own redline.
    edges = (
        [0.0]
        + [s.speed for s in shift_points(inputs)]
        + [_speed(inputs, inputs.max_rpm, inputs.gears[-1])]
    )

    # Downwards: the highest gear that caps anywhere is the last one that does.
    for i in range(len(inputs.gears) - 1, -1, -1):
        ratio = inputs.gears[i]
        low = edges[i]
        # Past the torque curve's last point there is no data, and no drawn trace.
        high = min(edges[i + 1], _speed(inputs, span_high, ratio))
        if high <= low:
            continue  # ratios entered out of order: this gear covers nothing

        # The topmost grid sample that is still capped, and the uncapped one above.
        capped = -1
        for k in range(scan + 1):
            if force(low + (high - low) * k / scan, ratio) > limit:
                capped = k
        if capped < 0:
            continue
        if capped == scan:
            # Still pulling harder than the tires can hold when the gear ends.
            return high, i + 1

        lo = low + (high - low) * capped / scan
        hi = low + (high - low) * (capped + 1) / scan
        for _ in range(60):
            mid = (lo + hi) / 2.0
            if force(mid, ratio) > limit:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0, i + 1

    return 0.0, 0


def _gear_from_shifts(shift_speeds: list[float], speed: float, n_gears: int) -> int:
    """:func:`gear_at_speed` against a precomputed list of shift speeds.

    Identical semantics — strictly ``<``, so at a shift speed the shift has
    already happened — but it does not rebuild every :class:`ShiftPoint` on each
    call, which an integrator sampling thousands of speeds cannot afford.
    """
    for i, shift_speed in enumerate(shift_speeds):
        if speed < shift_speed:
            return i + 1
    return n_gears


def _accel_run(inputs: Inputs, target_speed: float, steps: int) -> float | None:
    """Integrate ``dv / a`` from rest to ``target_speed``, ignoring shift dead time.

    Returns seconds, or ``None`` when there is no torque curve or the car tops out
    first. How much of the run was grip-capped is :func:`traction_limited`'s
    question, not this one's — asking it here would bound the answer by the
    benchmark speed rather than by the car.

    Everything is computed in SI regardless of the selected units, because
    ``a = F / m`` only holds with consistent ones: pounds-force over pounds-mass
    is off by ``g``. Seconds come out the same either way.
    """
    if not inputs.gears:
        raise ValueError("need at least one gear")
    if inputs.weight <= 0:
        raise ValueError("weight must be positive")
    if inputs.mu <= 0:
        raise ValueError("mu must be positive")
    if inputs.shift_time < 0:
        raise ValueError("shift_time must be non-negative")
    if target_speed <= 0:
        raise ValueError("target speed must be positive")
    if steps <= 0:
        raise ValueError("steps must be positive")

    if inputs.curve_span() is None:
        return None
    # Nothing below the top gear's redline is out of reach, and past it no amount
    # of integrating helps. Checking first means the loop below is always finite.
    if target_speed > max(_speed(inputs, inputs.max_rpm, r) for r in inputs.gears):
        return None

    metric = inputs.units == "metric"
    to_mps = MPS_PER_KMH if metric else MPS_PER_MPH
    force_to_n = 1.0 if metric else N_PER_LBF
    mass = inputs.weight if metric else inputs.weight * KG_PER_LB

    # The same cap the chart draws, in newtons. `mu * N` weighs the real mass, never
    # the inertia-inflated one below: friction acts on what presses the tires down,
    # not on what the drivetrain has to spin up.
    grip_force = traction_limit(inputs) * force_to_n

    shift_speeds = [s.speed for s in shift_points(inputs)]
    n_gears = len(inputs.gears)

    delta = target_speed * to_mps / steps
    seconds = 0.0
    for k in range(steps):
        # Midpoint, not trapezoid: 1/a jumps at every shift, and averaging the two
        # sides of a jump smears force from one gear into the next.
        speed_mps = (k + 0.5) * delta
        speed = speed_mps / to_mps

        gear = _gear_from_shifts(shift_speeds, speed, n_gears)
        ratio = inputs.gears[gear - 1]
        engine_force = _effort(inputs, ratio, _rpm(inputs, speed, ratio)) * force_to_n

        force = min(engine_force, grip_force)

        # Everything geared up behind the clutch has to be spun up too, and its
        # apparent mass grows with the square of the ratio it sits behind. This is
        # the standard road-load heuristic, not a measured moment of inertia, but
        # without it a shorter gear would look like free acceleration.
        spin = 1.04 + 0.0025 * overall_ratio(ratio, inputs.final_drive, inputs.transfer) ** 2
        seconds += delta / (force / (mass * spin))

    return seconds


def accel_time(inputs: Inputs, target_speed: float, steps: int = ACCEL_STEPS) -> float | None:
    """Seconds from rest to ``target_speed``, or ``None`` if the car never gets there.

    ``target_speed`` is in display units — km/h under metric, mph under imperial —
    but the answer is in seconds either way.
    """
    seconds = _accel_run(inputs, target_speed, steps)
    if seconds is None:
        return None
    shifts = sum(1 for s in shift_points(inputs) if s.speed < target_speed)
    return seconds + shifts * inputs.shift_time


def acceleration(inputs: Inputs) -> Accel | None:
    """The standing-start benchmark run: 0-100 km/h under metric, 0-60 mph under imperial.

    ``None`` when there is no torque curve, the way :func:`effort_curves` returns
    an empty list — with no engine data there is no run to time.

    What this models: engine torque through the gearing, a traction limit from
    ``mu``, dead time on each upshift, and the inertia of the spinning drivetrain.

    What it does not: aerodynamic drag, rolling resistance, drivetrain efficiency
    losses, and the torque converter. Each needs data the calculator never asks
    for — a frontal area and drag coefficient, a rolling-resistance coefficient, an
    efficiency map, a converter's K-factor — and inventing defaults for them would
    dress a gearing tool up as a vehicle-dynamics sim. Two consequences follow from
    their absence and are worth knowing. Nothing decelerates the car during a shift,
    so dead time simply adds. And in the traction-limited region mass cancels out of
    ``mu * m * g / (m * spin)``, so it is ``mu``, not weight, that sets the launch.

    Times are therefore optimistic in absolute terms. Comparing two gearsets on one
    car, which is what this calculator is for, they are sound.
    """
    if inputs.curve_span() is None:
        return None

    target = BENCHMARK_KMH if inputs.units == "metric" else BENCHMARK_MPH
    seconds = _accel_run(inputs, target, ACCEL_STEPS)
    shifts = sum(1 for s in shift_points(inputs) if s.speed < target)
    # Spans the whole rev range, so it stands even when the car never reaches the
    # benchmark and `time` is None. A car too slow for 0-100 still lights its tires.
    limited_to, limited_gear = traction_limited(inputs)
    return Accel(
        target_speed=target,
        time=None if seconds is None else seconds + shifts * inputs.shift_time,
        shifts=shifts,
        traction_limited_to=limited_to,
        traction_limited_gear=limited_gear,
    )


def accel_profile(
    data: dict, target_speed: float, steps: int = ACCEL_STEPS
) -> dict | None:
    """Speed-vs-time trace of a standing-start run to ``target_speed``: dict in, dict out.

    Returns ``{"speed": [...], "time": [...]}`` — display-unit speeds and the
    seconds-from-rest to reach each, both monotonic and index-aligned — or
    ``None`` when there is no run to trace (no torque curve, or the car tops out
    below ``target_speed``), the same condition as :func:`accel_time`.

    Integrated exactly like :func:`_accel_run`, but recording the cumulative time
    as it goes and charging a gear's dead time the moment the run crosses its
    shift point. So the time at any speed matches what :func:`accel_time` would
    return for it — which is what lets the on-screen accelerator ramp in real
    time: hold it for the setup's 0-100 and it arrives at 100.

    The output is decimated to a few hundred points (the integration itself stays
    full-resolution) — enough to interpolate smoothly without shipping every step.
    """
    inputs = Inputs.from_dict(data)
    if _accel_run(inputs, target_speed, steps) is None:
        return None

    metric = inputs.units == "metric"
    to_mps = MPS_PER_KMH if metric else MPS_PER_MPH
    force_to_n = 1.0 if metric else N_PER_LBF
    mass = inputs.weight if metric else inputs.weight * KG_PER_LB
    grip_force = traction_limit(inputs) * force_to_n

    shift_speeds = [s.speed for s in shift_points(inputs)]
    n_gears = len(inputs.gears)

    delta = target_speed * to_mps / steps
    keep = max(1, steps // 300)
    speeds = [0.0]
    times = [0.0]
    seconds = 0.0
    crossed = 0
    for k in range(steps):
        speed = (k + 0.5) * delta / to_mps
        gear = _gear_from_shifts(shift_speeds, speed, n_gears)
        ratio = inputs.gears[gear - 1]
        engine_force = _effort(inputs, ratio, _rpm(inputs, speed, ratio)) * force_to_n
        force = min(engine_force, grip_force)
        spin = 1.04 + 0.0025 * overall_ratio(ratio, inputs.final_drive, inputs.transfer) ** 2
        seconds += delta / (force / (mass * spin))

        v_end = (k + 1) * delta / to_mps
        # Dead time lands as the run passes each shift point, so it shows up in the
        # trace at the right speed rather than all bunched at the finish.
        while crossed < len(shift_speeds) and shift_speeds[crossed] < v_end:
            seconds += inputs.shift_time
            crossed += 1

        if (k + 1) % keep == 0 or k == steps - 1:
            speeds.append(v_end)
            times.append(seconds)

    return {"speed": speeds, "time": times}


@dataclass
class Result:
    """Computed output for a set of inputs."""

    units: Units
    speed_unit: str
    torque_unit: str
    power_unit: str
    force_unit: str
    max_rpm: float
    max_speed: float
    curves: list[GearCurve]
    shift_rpm: float
    shifts: list[ShiftPoint]
    trace: list[tuple[float, float]]
    spread: list[GearSpan] = field(default_factory=list)
    # All empty when no torque curve was supplied.
    engine: list[tuple[float, float, float]] = field(default_factory=list)
    efforts: list[EffortCurve] = field(default_factory=list)
    crossovers: list[Crossover] = field(default_factory=list)
    max_force: float = 0.0
    traction_limit: float = 0.0  # force the tires can hold, same unit as max_force
    peak_torque: tuple[float, float] | None = None  # (rpm, torque)
    peak_power: tuple[float, float] | None = None  # (rpm, power)
    acceleration: Accel | None = None  # None when no torque curve was supplied

    def to_dict(self) -> dict:
        """Plain-dict form for easy consumption from JS via ``.toJs()``."""
        return {
            "units": self.units,
            "speed_unit": self.speed_unit,
            "torque_unit": self.torque_unit,
            "power_unit": self.power_unit,
            "force_unit": self.force_unit,
            "max_rpm": self.max_rpm,
            "max_speed": self.max_speed,
            "curves": [
                {
                    "gear": c.gear,
                    "ratio": c.ratio,
                    "top_speed": c.top_speed,
                    "samples": c.samples,
                }
                for c in self.curves
            ],
            "shift_rpm": self.shift_rpm,
            "shifts": [
                {
                    "from_gear": s.from_gear,
                    "to_gear": s.to_gear,
                    "speed": s.speed,
                    "rpm_after": s.rpm_after,
                    "rpm_drop": s.rpm_drop,
                }
                for s in self.shifts
            ],
            "trace": self.trace,
            "spread": [
                {
                    "gear": g.gear,
                    "from_speed": g.from_speed,
                    "to_speed": g.to_speed,
                    "share": g.share,
                }
                for g in self.spread
            ],
            "engine": self.engine,
            "efforts": [
                {"gear": e.gear, "ratio": e.ratio, "samples": e.samples} for e in self.efforts
            ],
            "crossovers": [
                {
                    "from_gear": c.from_gear,
                    "to_gear": c.to_gear,
                    "speed": c.speed,
                    "rpm": c.rpm,
                    "rpm_after": c.rpm_after,
                    "force": c.force,
                    "at_redline": c.at_redline,
                }
                for c in self.crossovers
            ],
            "max_force": self.max_force,
            "traction_limit": self.traction_limit,
            "peak_torque": self.peak_torque,
            "peak_power": self.peak_power,
            "acceleration": None
            if self.acceleration is None
            else {
                "target_speed": self.acceleration.target_speed,
                "time": self.acceleration.time,
                "shifts": self.acceleration.shifts,
                "traction_limited_to": self.acceleration.traction_limited_to,
                "traction_limited_gear": self.acceleration.traction_limited_gear,
            },
        }


def gear_table(inputs: Inputs, step: float = 250.0) -> Result:
    """Compute speed-vs-RPM curves, top speeds, and shift points for every gear.

    Samples each gear from ``step`` up to ``max_rpm`` (inclusive) so the
    frontend can draw one polyline per gear plus a top-speed table, and adds the
    shift points and acceleration trace for the configured shift RPM.

    When ``inputs`` carries a torque curve, the tractive-effort curves, their
    crossovers, and the engine's own torque/power samples come along too.
    """
    if step <= 0:
        raise ValueError("step must be positive")
    metric = inputs.units == "metric"
    speed_unit = "km/h" if metric else "mph"

    # RPM sample points, always including max_rpm as the final point.
    points: list[float] = []
    rpm = step
    while rpm < inputs.max_rpm:
        points.append(rpm)
        rpm += step
    points.append(inputs.max_rpm)

    curves: list[GearCurve] = []
    max_speed = 0.0
    for i, ratio in enumerate(inputs.gears, start=1):
        samples = [(p, _speed(inputs, p, ratio)) for p in points]
        top = samples[-1][1]
        max_speed = max(max_speed, top)
        curves.append(GearCurve(gear=i, ratio=ratio, top_speed=top, samples=samples))

    engine = engine_samples(inputs)
    efforts = effort_curves(inputs)
    max_force = max((f for e in efforts for _, f in e.samples), default=0.0)
    peak_torque = max(((r, t) for r, t, _ in engine), key=lambda p: p[1], default=None)
    peak_power = max(((r, p) for r, _, p in engine), key=lambda p: p[1], default=None)

    return Result(
        units=inputs.units,
        speed_unit=speed_unit,
        torque_unit="N·m" if metric else "lb-ft",
        power_unit="kW" if metric else "hp",
        force_unit="N" if metric else "lbf",
        max_rpm=inputs.max_rpm,
        max_speed=max_speed,
        curves=curves,
        shift_rpm=inputs.effective_shift_rpm(),
        shifts=shift_points(inputs),
        trace=shift_trace(inputs),
        spread=gear_spread(inputs),
        engine=engine,
        efforts=efforts,
        crossovers=shift_crossovers(inputs),
        max_force=max_force,
        traction_limit=traction_limit(inputs),
        peak_torque=peak_torque,
        peak_power=peak_power,
        acceleration=acceleration(inputs),
    )


def compute(data: dict, step: float = 250.0) -> dict:
    """Convenience entrypoint for the browser: dict in, dict out."""
    return gear_table(Inputs.from_dict(data), step=step).to_dict()


def at_speed(data: dict, speed: float) -> dict:
    """Where the drivetrain sits at ``speed``: dict in, dict out.

    Resolves the gear from the shift schedule, then the engine RPM that gear
    needs to hold ``speed``. Together these place the chart marker on the shift
    trace for any speed in the run.

    ``torque``, ``power`` and ``force`` describe the engine's operating point
    there, and are all ``None`` together when there is no torque curve or the
    engine would be outside the range it covers — the latter happens below the
    curve's first point, where a real car would be slipping the clutch, and above
    the redline, where a setup is being asked for a speed it cannot reach.
    """
    inputs = Inputs.from_dict(data)
    gear = gear_at_speed(inputs, speed)
    ratio = inputs.gears[gear - 1]
    rpm = _rpm(inputs, speed, ratio)

    span = inputs.curve_span()
    torque = power = force = None
    if span is not None and span[0] <= rpm <= span[1]:
        torque = torque_at_rpm(inputs.torque_curve, rpm)
        power = power_at_rpm(torque, rpm, inputs.units)
        force = _effort(inputs, ratio, rpm)

    return {
        "gear": gear, "ratio": ratio, "rpm": rpm,
        "torque": torque, "power": power, "force": force,
    }


def gears_at_speeds(
    data: dict, speeds, times=None, lateral_g=None, corner_g=CORNER_LATERAL_G
) -> list:
    """The gear held at each of ``speeds``: dict in, list of 1-based gears out.

    ``speeds`` is a lap in *sample order* — the result is stateful, not a pure
    per-speed lookup. Accelerating, the driver follows the shift schedule
    exactly (same semantics as :func:`gear_at_speed`, including "at a shift
    speed the shift has just happened"). Decelerating, they hold the current
    gear until the gear below would actually pull harder — the same
    tractive-effort crossovers that define the optimal upshifts, read in the
    other direction — then step down through each crossover as speed falls.

    Holding above the crossover costs nothing (the taller gear is still the
    stronger one there), and the two thresholds together give the map real
    hysteresis: up at the schedule's speed, back down only at the crossover
    below it. The schedule's gear is the floor either way, so a held gear can
    never be asked for revs the schedule would not allow — but downshifts
    never take 1st: a braking zone stops at 2nd, because 1st on a moving car
    is a launch gear, not a corner gear. 1st only appears where the schedule
    itself starts a sample there.

    Two more pieces of laziness, each fed by an optional telemetry channel:

    - ``times`` (s, per sample): every downshift costs a shift and usually a
      shift back up, ``2 * shift_time`` of dead time in total. A downshift is
      only taken when the car will stay below the pair's upshift speed for at
      least that long; a briefer dip — a kink, a lift — is ridden out in the
      taller gear.
    - ``lateral_g`` (G, per sample): above ``corner_g`` (default
      ``CORNER_LATERAL_G``) the car is loaded up mid-corner, where nobody
      rows the box. Downshifts wait until the car unwinds; upshifts still
      follow the schedule, since the alternative is holding a gear past the
      revs the schedule allows.

    Without a usable torque curve there are no effort curves to cross, so the
    schedule times the downshifts instead — the dead-time and cornering
    holds still apply. Speeds are in the setup's unit system, like everything
    else.
    """
    inputs = Inputs.from_dict(data)
    speeds = [float(v) for v in speeds]
    if times is not None:
        times = [float(t) for t in times]
        if len(times) != len(speeds):
            times = None
    if lateral_g is not None:
        lateral_g = [abs(float(g)) for g in lateral_g]
        if len(lateral_g) != len(speeds):
            lateral_g = None
    corner_g = float(corner_g)

    shift_speeds = [s.speed for s in shift_points(inputs)]
    n_gears = len(inputs.gears)

    # crossover_speeds[g - 2] is where gear g - 1 starts out-pulling gear g;
    # decelerating past it in gear g is the moment the downshift pays.
    crossover_speeds = [c.speed for c in shift_crossovers(inputs)]
    round_trip = 2.0 * inputs.shift_time

    def dwell_pays(i: int, pair: int) -> bool:
        """Will a downshift through ``pair`` at sample ``i`` earn its dead time?

        The counterfactual upshift back comes at the pair's schedule speed, so
        the time spent below it is what the round trip buys.
        """
        if times is None:
            return True
        up_speed = shift_speeds[pair]
        for j in range(i + 1, len(speeds)):
            if speeds[j] >= up_speed:
                return times[j] - times[i] >= round_trip
        return True  # never back up within the lap: the shift pays for itself

    gears = []
    gear = 0  # below any real gear, so the first sample always follows the schedule
    for i, v in enumerate(speeds):
        scheduled = _gear_from_shifts(shift_speeds, v, n_gears)
        if scheduled >= gear:
            gear = scheduled
        elif lateral_g is not None and lateral_g[i] >= corner_g:
            pass  # loaded up mid-corner: the downshift waits for the exit
        else:
            floor = max(scheduled, min(2, n_gears))
            while (
                gear > floor
                and (not crossover_speeds or v < crossover_speeds[gear - 2])
                and dwell_pays(i, gear - 2)
            ):
                gear -= 1
        gears.append(gear)
    return gears
