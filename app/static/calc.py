"""Gearing calculator core math.

Pure standard-library Python so the exact same module runs unmodified under
Pyodide in the browser *and* is importable by pytest on the server. There is no
FastAPI / web dependency here — just the drivetrain math.

The model relates engine RPM to road speed through the drivetrain::

    overall_ratio = gear_ratio * final_drive * transfer_case
    speed = rpm * tire_circumference / overall_ratio * (1 - slip)  (per minute)

with unit conversions folded in for mph (tire diameter in inches) and km/h
(tire diameter in mm).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import pi

# Imperial constant: mph = rpm * tire_dia_in / (overall_ratio * MPH_CONST).
# Derivation: inches/hour = rpm * pi * dia * 60; miles/hour divides by 63360,
# so MPH_CONST = 63360 / (pi * 60) = 336.135...
MPH_CONST = 63360.0 / (pi * 60.0)

Units = str  # "imperial" | "metric"


def _validate_units(units: Units) -> None:
    if units not in ("imperial", "metric"):
        raise ValueError(f"units must be 'imperial' or 'metric', got {units!r}")


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
    units: Units = "imperial",
) -> float:
    """Road speed at a given engine ``rpm`` in the selected gear.

    ``tire`` is the tire *diameter* — inches for imperial (returns mph), or
    millimetres for metric (returns km/h). ``slip`` is torque-converter slip as
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
    units: Units = "imperial",
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
class Inputs:
    """All calculator inputs; keeps the JS<->Python boundary explicit."""

    gears: list[float]
    final_drive: float = 3.9
    transfer: float = 1.0
    tire: float = 25.0
    slip: float = 0.0
    max_rpm: float = 7000.0
    units: Units = "imperial"

    @classmethod
    def from_dict(cls, data: dict) -> "Inputs":
        """Build from a plain dict (e.g. converted from a JS object)."""
        return cls(
            gears=[float(g) for g in data["gears"] if float(g) > 0],
            final_drive=float(data.get("final_drive", 3.9)),
            transfer=float(data.get("transfer", 1.0)),
            tire=float(data.get("tire", 25.0)),
            slip=float(data.get("slip", 0.0)),
            max_rpm=float(data.get("max_rpm", 7000.0)),
            units=data.get("units", "imperial"),
        )


@dataclass
class Result:
    """Computed output for a set of inputs."""

    units: Units
    speed_unit: str
    max_rpm: float
    max_speed: float
    curves: list[GearCurve]

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
        }


def gear_table(inputs: Inputs, step: float = 250.0) -> Result:
    """Compute speed-vs-RPM curves and top speeds for every gear.

    Samples each gear from ``step`` up to ``max_rpm`` (inclusive) so the
    frontend can draw one polyline per gear plus a top-speed table.
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
        samples = [
            (
                p,
                speed_at_rpm(
                    p,
                    ratio,
                    inputs.final_drive,
                    inputs.transfer,
                    inputs.tire,
                    inputs.slip,
                    inputs.units,
                ),
            )
            for p in points
        ]
        top = samples[-1][1]
        max_speed = max(max_speed, top)
        curves.append(GearCurve(gear=i, ratio=ratio, top_speed=top, samples=samples))

    return Result(
        units=inputs.units,
        speed_unit=speed_unit,
        max_rpm=inputs.max_rpm,
        max_speed=max_speed,
        curves=curves,
    )


def compute(data: dict, step: float = 250.0) -> dict:
    """Convenience entrypoint for the browser: dict in, dict out."""
    return gear_table(Inputs.from_dict(data), step=step).to_dict()
