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
NM_PER_LBFT = 1.3558179483314004

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
    peak_torque: tuple[float, float] | None = None  # (rpm, torque)
    peak_power: tuple[float, float] | None = None  # (rpm, power)

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
            "peak_torque": self.peak_torque,
            "peak_power": self.peak_power,
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
        peak_torque=peak_torque,
        peak_power=peak_power,
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
