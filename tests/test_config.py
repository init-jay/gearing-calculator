"""Guard the runtime config that decides where Pyodide is loaded from.

``app/static/config.json`` is read by two independent consumers — the browser
(``app.js``) and the vendoring script — and a disagreement between them is the
kind of thing that only shows up as a blank page in production.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
CONFIG = json.loads((STATIC / "config.json").read_text(encoding="utf-8"))
PYODIDE = CONFIG["pyodide"]

INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
VENDOR = (ROOT / "scripts" / "vendor_pyodide.py").read_text(encoding="utf-8")


def test_config_has_the_keys_app_js_reads():
    assert set(PYODIDE) == {"source", "version", "vendored", "cdn"}


def test_source_is_one_of_the_two_supported_values():
    assert PYODIDE["source"] in ("vendored", "cdn")


def test_vendoring_is_consistent_with_the_chosen_source():
    # Not a check that the source *is* "vendored" — which one you deploy with is a
    # judgement call, not an invariant. The invariant is that "vendored" without a
    # runtime on disk is a broken deploy, and the only warning would be a blank page.
    on_disk = (STATIC / "pyodide" / "pyodide.js").is_file()
    if PYODIDE["source"] == "vendored":
        assert on_disk, "source is 'vendored' but app/static/pyodide/ is missing"


def test_version_looks_like_a_pinned_release():
    assert re.fullmatch(r"\d+\.\d+\.\d+", PYODIDE["version"])


def test_vendored_path_is_same_origin_and_a_directory():
    # A scheme here would quietly reintroduce a third-party fetch under a name
    # that says otherwise.
    assert "://" not in PYODIDE["vendored"]
    assert PYODIDE["vendored"].endswith("/")


def test_cdn_url_is_https_and_carries_the_version_placeholder():
    # Substituting {version} at load time is what keeps the CDN and the vendored
    # copy on the same Pyodide release.
    assert PYODIDE["cdn"].startswith("https://")
    assert "{version}" in PYODIDE["cdn"]
    assert PYODIDE["cdn"].endswith("/")
    assert PYODIDE["version"] not in PYODIDE["cdn"]  # pinned once, not twice


def test_cdn_url_resolves_to_a_plausible_release():
    resolved = PYODIDE["cdn"].replace("{version}", PYODIDE["version"])
    assert resolved == "https://cdn.jsdelivr.net/pyodide/v0.28.0/full/"


def test_the_vendoring_script_reads_the_version_from_the_config():
    # If it kept its own DEFAULT_VERSION, the two could drift silently.
    assert "DEFAULT_VERSION" not in VENDOR
    assert 'json.load(fh)["pyodide"]' in VENDOR


@pytest.mark.skipif(
    not (STATIC / "pyodide" / "package.json").is_file(),
    reason="Pyodide is not vendored here (run scripts/vendor_pyodide.py)",
)
def test_the_vendored_copy_is_the_pinned_version():
    on_disk = json.loads((STATIC / "pyodide" / "package.json").read_text(encoding="utf-8"))
    assert on_disk["version"] == PYODIDE["version"]


def test_index_html_does_not_hardcode_the_runtime():
    # A <script src="pyodide/pyodide.js"> in the page would load the vendored copy
    # regardless of config.json, and the "cdn" setting would silently do nothing.
    assert "pyodide/pyodide.js" not in INDEX
    assert "loadPyodide" not in INDEX


def test_app_js_resolves_the_runtime_through_the_config():
    assert 'fetch("config.json")' in APP_JS
    assert "pyodideBaseUrl" in APP_JS
    # The old hardcoded indexURL must not survive anywhere.
    assert 'indexURL: "pyodide/"' not in APP_JS
