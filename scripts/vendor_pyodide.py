#!/usr/bin/env python3
"""Download the Pyodide runtime and vendor it into ``app/static/pyodide/``.

The site loads Pyodide from its own origin — nothing is fetched from a CDN at
runtime — so the runtime has to live on disk. It is too large to commit, so it
is gitignored and re-fetched with this script:

    uv run scripts/vendor_pyodide.py

Standard library only, so it runs before any dependencies are installed.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# Pinned for reproducibility. Bump deliberately.
DEFAULT_VERSION = "0.28.0"
RELEASE_URL = (
    "https://github.com/pyodide/pyodide/releases/download/"
    "{version}/pyodide-core-{version}.tar.bz2"
)

DEST = Path(__file__).resolve().parent.parent / "app" / "static" / "pyodide"

# The loader needs these; the rest of pyodide-core is small enough to keep.
REQUIRED = ["pyodide.js", "pyodide.asm.js", "pyodide.asm.wasm", "python_stdlib.zip"]


def download(url: str, dest: Path) -> None:
    print(f"Downloading {url}")
    with urllib.request.urlopen(url) as resp, dest.open("wb") as out:  # noqa: S310
        total = int(resp.headers.get("Content-Length", 0))
        read = 0
        while chunk := resp.read(1 << 16):
            out.write(chunk)
            read += len(chunk)
            if total:
                print(f"\r  {read / 1e6:.1f} / {total / 1e6:.1f} MB", end="", flush=True)
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--force", action="store_true", help="re-vendor even if present")
    args = parser.parse_args()

    if DEST.exists() and not args.force:
        print(f"{DEST} already exists; use --force to re-vendor.")
        return 0

    url = RELEASE_URL.format(version=args.version)

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        tarball = tmpdir / "pyodide-core.tar.bz2"
        try:
            download(url, tarball)
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            print(f"error: could not download {url} ({exc})", file=sys.stderr)
            print("Check that the pinned version exists on the releases page.", file=sys.stderr)
            return 1

        print("Extracting…")
        with tarfile.open(tarball) as tf:
            tf.extractall(tmpdir, filter="data")

        extracted = tmpdir / "pyodide"
        if not extracted.is_dir():
            print(f"error: expected a 'pyodide/' dir in {tarball.name}", file=sys.stderr)
            return 1

        missing = [f for f in REQUIRED if not (extracted / f).is_file()]
        if missing:
            print(f"error: release is missing {', '.join(missing)}", file=sys.stderr)
            return 1

        if DEST.exists():
            shutil.rmtree(DEST)
        DEST.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(extracted, DEST)

    size = sum(f.stat().st_size for f in DEST.rglob("*") if f.is_file())
    print(f"Vendored Pyodide {args.version} -> {DEST} ({size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
