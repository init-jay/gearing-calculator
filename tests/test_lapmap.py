"""Tests for the RaceChrono lap-map parser.

Like ``calc.py``, the module lives under ``app/static/`` because it is also
served to and loaded by Pyodide in the browser; it is loaded here by file path.
"""

import importlib.util
import sys
from math import cos, isclose, radians
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

spec = importlib.util.spec_from_file_location("lapmap", STATIC / "lapmap.py")
lapmap = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["lapmap"] = lapmap
spec.loader.exec_module(lapmap)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "racechrono_format3.csv"
TEXT = FIXTURE.read_text()


@pytest.fixture
def parsed():
    return lapmap.parse_racechrono(TEXT)


def test_parses_laps_and_metadata(parsed):
    assert parsed["session"] == "Luddenham Raceway"
    assert parsed["track"] == "Luddenham Raceway"
    assert [lap["lap"] for lap in parsed["laps"]] == [1, 2, 3]
    # Lap 2 has 6 data rows, one of which has a GPS dropout (blank lat/lon).
    assert len(parsed["laps"][1]["x"]) == 5


def test_partial_flags(parsed):
    complete = {lap["lap"]: lap["complete"] for lap in parsed["laps"]}
    assert complete == {1: False, 2: True, 3: False}


def test_duration_and_time_offsets(parsed):
    lap2 = parsed["laps"][1]
    assert isclose(lap2["duration"], 25.5 - 20.0)
    assert lap2["t"][0] == 0.0
    assert isclose(lap2["t"][-1], lap2["duration"])


def test_first_duplicate_speed_column_wins(parsed):
    # The trailing duplicate "speed" column holds GPS speed + 100 in the
    # fixture, so any value >= 100 means the wrong column was read.
    lap2 = parsed["laps"][1]
    assert lap2["speed"] == [20.0, 25.0, 30.0, 35.0, 40.0]
    assert lap2["speed_min"] == 20.0
    assert lap2["speed_max"] == 40.0


def test_skips_prelap_and_dropout_rows(parsed):
    # The row with a blank lap_number and the blank-lat/lon row are dropped;
    # every remaining sample pairs x with y and speed.
    for lap in parsed["laps"]:
        assert len(lap["x"]) == len(lap["y"]) == len(lap["speed"]) == len(lap["t"])


def test_projection_equirectangular(parsed):
    # Lap 1 moves +0.0001 deg in both lat and lon per point. East (x) spans
    # dlon * cos(lat0) * 111320; north (y) spans dlat * 110540, increasing
    # northward (toward less-negative latitude).
    lap1 = parsed["laps"][0]
    dx = lap1["x"][-1] - lap1["x"][0]
    dy = lap1["y"][-1] - lap1["y"][0]
    assert isclose(dx, 0.0002 * cos(radians(-33.8582)) * 111_320.0, rel_tol=1e-3)
    assert isclose(dy, -0.0002 * 110_540.0, rel_tol=1e-3)


def test_laps_share_one_origin(parsed):
    # Lap 1 and lap 3 revisit the same GPS coordinates, so their projected
    # points must coincide — one shared origin for the whole file.
    lap1, lap3 = parsed["laps"][0], parsed["laps"][2]
    assert isclose(lap1["x"][1], lap3["x"][-1], abs_tol=1e-9)
    assert isclose(lap1["y"][1], lap3["y"][-1], abs_tol=1e-9)


def test_thinning_caps_points_and_keeps_last():
    parsed = lapmap.parse_racechrono(TEXT, max_points_per_lap=2)
    lap2 = parsed["laps"][1]
    full = lapmap.parse_racechrono(TEXT)["laps"][1]
    assert len(lap2["x"]) <= 3  # ceil-stride decimation may keep one extra
    assert lap2["x"][-1] == full["x"][-1]
    assert lap2["speed"][-1] == full["speed"][-1]


def test_rejects_garbage():
    with pytest.raises(ValueError, match="header row"):
        lapmap.parse_racechrono("hello,world\n1,2\n")


def test_rejects_unsupported_format():
    with pytest.raises(ValueError, match="format 2"):
        lapmap.parse_racechrono(TEXT.replace("Format,3", "Format,2"))


def test_rejects_missing_column():
    with pytest.raises(ValueError, match="latitude"):
        lapmap.parse_racechrono(TEXT.replace("latitude", "lat_removed"))


def test_rejects_no_data_rows():
    header_end = TEXT.index("unix time")
    with pytest.raises(ValueError, match="No GPS lap data"):
        lapmap.parse_racechrono(TEXT[:header_end])


def test_single_lap_file_is_partial():
    lines = [
        line
        for line in TEXT.splitlines()
        if not (line and line[0].isdigit() and ",2," in line[:20])
        and not (line and line[0].isdigit() and ",3," in line[:20])
    ]
    parsed = lapmap.parse_racechrono("\n".join(lines))
    assert [lap["lap"] for lap in parsed["laps"]] == [1]
    assert parsed["laps"][0]["complete"] is False


def test_handles_bom_and_crlf():
    text = "\ufeff" + TEXT.replace("\n", "\r\n")
    parsed = lapmap.parse_racechrono(text)
    assert [lap["lap"] for lap in parsed["laps"]] == [1, 2, 3]


def test_lateral_g_extracted_per_sample(parsed):
    # Lap 2's five kept samples carry the lateral_acc column verbatim; the
    # GPS-dropout row is gone, so its -0.1 does not appear between them.
    assert parsed["laps"][1]["lat_g"] == [-0.1, 0.85, -0.1, -1.1, -0.1]
    for lap in parsed["laps"]:
        assert len(lap["lat_g"]) == len(lap["speed"])


def test_missing_lateral_column_reads_as_zero():
    parsed = lapmap.parse_racechrono(TEXT.replace("lateral_acc", "other_col"))
    assert all(g == 0.0 for lap in parsed["laps"] for g in lap["lat_g"])
