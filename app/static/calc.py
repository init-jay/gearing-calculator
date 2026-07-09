"""Gearing calculator core math.

Pure standard-library Python so the exact same module runs unmodified under
Pyodide in the browser *and* is importable by pytest on the server. There is no
FastAPI / web dependency here — just the drivetrain math.

The model relates engine RPM to road speed through the drivetrain::

    overall_ratio = gear_ratio * final_drive * transfer_case
    speed = rpm * tire_circumference / overall_ratio * (1 - slip)  (per minute)

with unit conversions folded in for km/h (tire diameter in mm, the default) and
mph (tire diameter in inches).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import pi

MM_PER_INCH = 25.4

# Imperial constant: mph = rpm * tire_dia_in / (overall_ratio * MPH_CONST).
# Derivation: inches/hour = rpm * pi * dia * 60; miles/hour divides by 63360,
# so MPH_CONST = 63360 / (pi * 60) = 336.135...
MPH_CONST = 63360.0 / (pi * 60.0)

Units = str  # "imperial" | "metric"


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
class Inputs:
    """All calculator inputs; keeps the JS<->Python boundary explicit."""

    gears: list[float]
    final_drive: float = 3.9
    transfer: float = 1.0
    tire: float = DEFAULT_TIRE_MM
    slip: float = 0.0
    max_rpm: float = 7000.0
    units: Units = "metric"
    shift_rpm: float | None = None  # None => shift at the redline

    def effective_shift_rpm(self) -> float:
        """The shift RPM actually used: defaults to, and never exceeds, redline."""
        if self.shift_rpm is None:
            return self.max_rpm
        if self.shift_rpm <= 0:
            raise ValueError("shift_rpm must be positive")
        return min(self.shift_rpm, self.max_rpm)

    @classmethod
    def from_dict(cls, data: dict) -> "Inputs":
        """Build from a plain dict (e.g. converted from a JS object)."""
        shift_rpm = data.get("shift_rpm")
        return cls(
            gears=[float(g) for g in data["gears"] if float(g) > 0],
            final_drive=float(data.get("final_drive", 3.9)),
            transfer=float(data.get("transfer", 1.0)),
            tire=float(data.get("tire", DEFAULT_TIRE_MM)),
            slip=float(data.get("slip", 0.0)),
            max_rpm=float(data.get("max_rpm", 7000.0)),
            units=data.get("units", "metric"),
            shift_rpm=None if shift_rpm is None else float(shift_rpm),
        )


def _speed(inputs: Inputs, rpm: float, ratio: float) -> float:
    """``speed_at_rpm`` with the drivetrain arguments bound to ``inputs``."""
    return speed_at_rpm(
        rpm, ratio, inputs.final_drive, inputs.transfer, inputs.tire, inputs.slip, inputs.units
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


@dataclass
class Result:
    """Computed output for a set of inputs."""

    units: Units
    speed_unit: str
    max_rpm: float
    max_speed: float
    curves: list[GearCurve]
    shift_rpm: float
    shifts: list[ShiftPoint]
    trace: list[tuple[float, float]]

    def to_dict(self) -> dict:
        """Plain-dict form for easy consumption from JS via ``.toJs()``."""
        return {
            "units": self.units,
            "speed_unit": self.speed_unit,
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
        }


def gear_table(inputs: Inputs, step: float = 250.0) -> Result:
    """Compute speed-vs-RPM curves, top speeds, and shift points for every gear.

    Samples each gear from ``step`` up to ``max_rpm`` (inclusive) so the
    frontend can draw one polyline per gear plus a top-speed table, and adds the
    shift points and acceleration trace for the configured shift RPM.
    """
    if step <= 0:
        raise ValueError("step must be positive")
    speed_unit = "mph" if inputs.units == "imperial" else "km/h"

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

    return Result(
        units=inputs.units,
        speed_unit=speed_unit,
        max_rpm=inputs.max_rpm,
        max_speed=max_speed,
        curves=curves,
        shift_rpm=inputs.effective_shift_rpm(),
        shifts=shift_points(inputs),
        trace=shift_trace(inputs),
    )


def compute(data: dict, step: float = 250.0) -> dict:
    """Convenience entrypoint for the browser: dict in, dict out."""
    return gear_table(Inputs.from_dict(data), step=step).to_dict()
