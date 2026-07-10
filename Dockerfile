# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Must match the runtime WORKDIR. uv bakes this path into the venv's scripts as
# a shebang, so a venv built elsewhere and copied here fails to exec.
WORKDIR /app

# Dependencies resolve in their own layer, so editing app/ does not re-install
# them. The project itself has no [build-system] and is never installed; the
# runtime stage runs it from the source tree.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY app/ ./app/
COPY scripts/ ./scripts/

# config.json decides where the browser loads Pyodide from, so it decides
# whether the runtime has to be in the image. Under "vendored" the site serves
# it from its own origin and it must be baked in; under "cdn" the browser
# fetches it and baking it in would add ~12 MB that nothing reads.
RUN <<'PY' python3
import json, subprocess, sys
source = json.load(open("app/static/config.json"))["pyodide"]["source"]
if source == "vendored":
    subprocess.run([sys.executable, "scripts/vendor_pyodide.py"], check=True)
else:
    print(f'pyodide.source is "{source}"; skipping vendoring')
PY


FROM python:3.12-slim-bookworm

# The server is a read-only static-file mount: no state, no shell, no writes.
RUN useradd --create-home --uid 1000 app

WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv/ ./.venv/
COPY --from=builder --chown=app:app /app/app/ ./app/

USER app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# app.main.main() binds 127.0.0.1 with --reload, which is right for a laptop and
# wrong for a container: it would refuse every connection from outside.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
