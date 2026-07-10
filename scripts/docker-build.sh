#!/usr/bin/env bash
#
# Build the gearing-calculator image.
#
# This devcontainer runs the docker-outside-of-docker feature, so there is no
# daemon in here: the `docker` CLI talks to the *host's* daemon over the mounted
# /var/run/docker.sock. Two consequences worth knowing:
#
#   * The build context is streamed from this container, so the paths below are
#     container paths and resolve normally.
#   * The image, and anything run from it, lands on the host. A published port
#     is reachable on the host, not on this container's localhost.
#
# Usage:
#   scripts/docker-build.sh                 # -> registry.kranky.dev/gearing-calculator:latest
#   IMAGE=ghcr.io/me/gc TAG=v1 scripts/docker-build.sh
#   scripts/docker-build.sh --no-cache      # extra args go to `docker build`
set -euo pipefail

IMAGE="${IMAGE:-registry.kranky.dev/gearing-calculator}"
TAG="${TAG:-latest}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! docker info >/dev/null 2>&1; then
  echo "error: no Docker daemon reachable." >&2
  echo "Check that /var/run/docker.sock is mounted and that you can read it:" >&2
  echo "    ls -l /var/run/docker.sock" >&2
  exit 1
fi

docker build --tag "$IMAGE:$TAG" --file "$ROOT/Dockerfile" "$@" "$ROOT"

cat <<EOF

Built $IMAGE:$TAG

Run it:
    docker run --rm -p 8000:8000 $IMAGE:$TAG

That publishes port 8000 on the Docker host. From inside this devcontainer,
http://localhost:8000 does not reach it — read it from the host's browser, or
hit the container directly:

    docker run --rm -d -p 8000:8000 --name gc $IMAGE:$TAG
    curl -sI "http://\$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' gc):8000/"
EOF
