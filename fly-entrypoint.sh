#!/bin/sh
# Fly attaches the `tret_storage` volume at /data (fly.toml `[mounts]`) after
# the image is built, so its ownership cannot be fixed by a Dockerfile RUN —
# there is nothing mounted there yet at build time, and a freshly attached Fly
# volume (or the first boot of a brand new one) comes back root-owned. The app
# itself runs as the non-root `tret` user (docs/hardening.md: "container/VM
# with a non-root user"), so root's only job here is to fix that ownership
# once per boot and then get out of the way.
#
# `setpriv` replaces this process via execve — no forked child — so the
# eventual uvicorn process becomes PID 1 in every way that matters: signals
# Fly/Docker send to the container reach it directly, with nothing sitting
# above it to relay or swallow them.
set -e

STORAGE_DIR="${TRET_STORAGE_DIR:-/data/storage}"
mkdir -p "$STORAGE_DIR"
# Recursive: on the first boot after this change, files already on the volume
# were written by root (the old all-root image) and need to change hands too.
# Cheap in steady state — new files already belong to `tret` — but correct
# even when they don't.
chown -R tret:tret "$(dirname "$STORAGE_DIR")"

exec setpriv --reuid=tret --regid=tret --clear-groups "$@"
