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
#   scripts/docker-build.sh                    # -> registry.kranky.dev/gearing-calculator:latest, linux/amd64
#   IMAGE=ghcr.io/me/gc TAG=v1 scripts/docker-build.sh
#   PLATFORM=linux/arm64 scripts/docker-build.sh   # native build, e.g. to run it here
#   scripts/docker-build.sh --no-cache         # extra args go to `docker build`
set -euo pipefail

IMAGE="${IMAGE:-registry.kranky.dev/gearing-calculator}"
TAG="${TAG:-latest}"
# Deploy targets are x86, so pin the platform rather than inheriting the build
# host's. On an arm64 machine this cross-builds under qemu emulation: slower,
# but it keeps an arm64 image from ever reaching an amd64 host.
PLATFORM="${PLATFORM:-linux/amd64}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! docker info >/dev/null 2>&1; then
  echo "error: no Docker daemon reachable." >&2
  echo "Check that /var/run/docker.sock is mounted and that you can read it:" >&2
  echo "    ls -l /var/run/docker.sock" >&2
  exit 1
fi

docker build \
  --platform "$PLATFORM" \
  --tag "$IMAGE:$TAG" \
  --file "$ROOT/Dockerfile" \
  "$@" "$ROOT"

cat <<EOF

Built $IMAGE:$TAG ($PLATFORM)

Serve it locally:
    scripts/docker-serve.sh

Push it:
    docker login registry.kranky.dev && docker push $IMAGE:$TAG
EOF
