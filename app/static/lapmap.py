"""RaceChrono lap-log parsing for the lap map.

Pure standard-library Python so the exact same module runs unmodified under
Pyodide in the browser *and* is importable by pytest on the server, mirroring
``calc.py``. The browser hands over the raw CSV text of a RaceChrono Pro
"Format 3" export; this module returns per-lap GPS traces projected to local
meters plus the speed samples used to shade the line.

A Format 3 file looks like::

    This file is created using RaceChrono Pro v10.2.4 ( ... ).
    Format,3
    Session title,"Luddenham Raceway"
    ...more metadata rows...
    timestamp,fragment_id,lap_number,...,latitude,longitude,speed,...
    unix time,,,s,m,...          <- units row
    ,,,,,100: gps,...,calc,...   <- source row
    1783144124.15,0,3,...        <- 20 Hz data rows

Some column names appear twice (``speed``, ``device_update_rate``); the first
occurrence is the GPS-sourced one and is the one used here.
"""

from __future__ import annotations

import csv
import io
from math import ceil, cos, radians

# Meters per degree at the equator. Longitude degrees shrink with cos(latitude);
# latitude degrees are treated as constant. Good to well under a meter across a
# race track, which is all the equirectangular projection needs to be.
M_PER_DEG_LON = 111_320.0
M_PER_DEG_LAT = 110_540.0

REQUIRED_COLUMNS = ("lap_number", "latitude", "longitude", "speed", "elapsed_time")

SUPPORTED_FORMAT = "3"


def parse_racechrono(text: str, max_points_per_lap: int = 1200) -> dict:
    """Parse a RaceChrono Pro Format 3 CSV export into per-lap GPS traces.

    Returns a dict of plain lists and scalars (safe to hand to ``toJs``)::

        {
          "session": str, "track": str,
          "laps": [
            {"lap": int, "complete": bool, "duration": float,
             "x": [m...], "y": [m...],       # local meters, shared origin
             "speed": [m/s...], "t": [s...],  # t is offset from lap start
             "lat_g": [G...],                 # lateral acc; 0.0 if not logged
             "speed_min": float, "speed_max": float},
            ...  # sorted by lap number
          ],
        }

    Raises ``ValueError`` with a human-readable message on anything that is
    not a usable Format 3 file.
    """
    rows = csv.reader(io.StringIO(text.lstrip("\ufeff")))

    session = ""
    track = ""
    header = None
    for row in rows:
        if not row:
            continue
        key = row[0].strip()
        if key == "timestamp":
            header = row
            break
        value = row[1].strip() if len(row) > 1 else ""
        if key == "Format" and value != SUPPORTED_FORMAT:
            raise ValueError(
                f"Unsupported RaceChrono format {value or '?'}"
                f" (expected {SUPPORTED_FORMAT})"
            )
        elif key == "Session title":
            session = value
        elif key == "Track name":
            track = value
    if header is None:
        raise ValueError("Not a RaceChrono CSV: no 'timestamp' header row found")

    # list.index returns the *first* occurrence, which picks the GPS-sourced
    # speed over the trailing calculated duplicate.
    missing = [name for name in REQUIRED_COLUMNS if name not in header]
    if missing:
        raise ValueError("Missing column(s): " + ", ".join(missing))
    col = {name: header.index(name) for name in REQUIRED_COLUMNS}
    width = max(col.values()) + 1
    # Lateral G is auxiliary (the gear simulation uses it to spot corners), so
    # a file without the column still parses — it just reads as zero.
    lat_g_col = header.index("lateral_acc") if "lateral_acc" in header else None

    # lap number -> [(lat, lon, speed, elapsed_time), ...] in file order. The
    # units and source rows that follow the header have a non-numeric (or
    # empty) first cell, so the float() gate below skips them along with any
    # blank or truncated lines.
    laps: dict[int, list] = {}
    lap_order: list[int] = []
    for row in rows:
        if len(row) < width:
            continue
        try:
            float(row[0])
            lap = int(row[col["lap_number"]])
            lat = float(row[col["latitude"]])
            lon = float(row[col["longitude"]])
            speed = float(row[col["speed"]])
            t = float(row[col["elapsed_time"]])
        except ValueError:
            continue  # units/source row, blank lap number, or GPS dropout
        lat_g = 0.0
        if lat_g_col is not None:
            try:
                lat_g = float(row[lat_g_col])
            except (ValueError, IndexError):
                pass  # a dropped calc cell should not cost the GPS sample
        if lap not in laps:
            laps[lap] = []
            lap_order.append(lap)
        laps[lap].append((lat, lon, speed, t, lat_g))

    laps = {n: pts for n, pts in laps.items() if len(pts) >= 2}
    if not laps:
        raise ValueError("No GPS lap data found")

    # The log starts and ends mid-lap, so the first and last lap numbers seen
    # in the file are partial. (A one-lap file is therefore all partial.)
    first_seen = next(n for n in lap_order if n in laps)
    last_seen = next(n for n in reversed(lap_order) if n in laps)

    # One shared origin so every lap lands in the same coordinate frame.
    all_pts = [p for pts in laps.values() for p in pts]
    lat0 = sum(p[0] for p in all_pts) / len(all_pts)
    lon0 = sum(p[1] for p in all_pts) / len(all_pts)

    out = []
    for lap in sorted(laps):
        pts = _thin(laps[lap], max_points_per_lap)
        xs, ys = _project(
            [p[0] for p in pts], [p[1] for p in pts], lat0, lon0
        )
        speeds = [p[2] for p in pts]
        t0 = pts[0][3]
        out.append(
            {
                "lap": lap,
                "complete": lap not in (first_seen, last_seen),
                "duration": pts[-1][3] - t0,
                "x": xs,
                "y": ys,
                "speed": speeds,
                "t": [p[3] - t0 for p in pts],
                "lat_g": [p[4] for p in pts],
                "speed_min": min(speeds),
                "speed_max": max(speeds),
            }
        )
    return {"session": session, "track": track, "laps": out}


def _project(lats: list, lons: list, lat0: float, lon0: float) -> tuple:
    """Equirectangular projection to meters east/north of (lat0, lon0)."""
    scale_x = cos(radians(lat0)) * M_PER_DEG_LON
    xs = [(lon - lon0) * scale_x for lon in lons]
    ys = [(lat - lat0) * M_PER_DEG_LAT for lat in lats]
    return xs, ys


def _thin(pts: list, max_points: int) -> list:
    """Decimate to at most ~max_points by stride, always keeping the last point."""
    if len(pts) <= max_points:
        return pts
    stride = ceil(len(pts) / max_points)
    thinned = pts[::stride]
    if thinned[-1] is not pts[-1]:
        thinned.append(pts[-1])
    return thinned
