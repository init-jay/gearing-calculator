#!/usr/bin/env bash
#
# Serve the built image for local testing.
#
# Under docker-outside-of-docker the app container is a *sibling* of this
# devcontainer, started on the host's daemon. So `-p` publishes the port on the
# host — which is where your browser is — and http://localhost:PORT just works
# there, with no VS Code port forwarding involved.
#
# The catch is that this devcontainer's own localhost is a different namespace
# and cannot see that port. From in here, reach the server via the default
# gateway (the host) instead; the script prints the URL.
#
# Usage:
#   scripts/docker-serve.sh              # foreground, Ctrl-C to stop
#   scripts/docker-serve.sh -d           # detached; stop with `docker rm -f gearing-calculator`
#   PORT=9000 scripts/docker-serve.sh    # publish on a different host port
set -euo pipefail

IMAGE="${IMAGE:-registry.kranky.dev/gearing-calculator}"
TAG="${TAG:-latest}"
NAME="${NAME:-gearing-calculator}"
PORT="${PORT:-8000}"

if ! docker image inspect "$IMAGE:$TAG" >/dev/null 2>&1; then
  echo "error: image $IMAGE:$TAG not found. Build it first:" >&2
  echo "    scripts/docker-build.sh" >&2
  exit 1
fi

docker rm -f "$NAME" >/dev/null 2>&1 || true

# The image always serves on 8000 internally; PORT only picks the host port.
echo "Serving $IMAGE:$TAG"
echo "  host browser:      http://localhost:$PORT"
if GW="$(ip route 2>/dev/null | awk '/^default/{print $3; exit}')" && [ -n "$GW" ]; then
  echo "  from devcontainer: http://$GW:$PORT"
fi
echo

exec docker run --rm --name "$NAME" --publish "$PORT:8000" "$@" "$IMAGE:$TAG"
