#!/bin/bash
# Stops tret. Your data is kept — double-click start-tret to come back.
set -u
cd "$(dirname "$0")" || exit 1

compose() {
  if docker compose version >/dev/null 2>&1; then docker compose "$@"
  else docker-compose "$@"
  fi
}

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo ""
  echo "Docker isn't running, so tret is already stopped. Nothing to do."
  echo ""
  read -r -p "Press Enter to close this window... " _ 2>/dev/null || true
  exit 0
fi

echo ""
echo "Stopping tret..."
if compose down; then
  echo ""
  echo "tret is stopped. All your data is kept safe — just double-click"
  echo "start-tret whenever you want to come back."
else
  echo ""
  echo "Something went wrong while stopping. You can try again, or run"
  echo "'docker compose down' from a terminal in this folder."
fi
echo ""
read -r -p "Press Enter to close this window... " _ 2>/dev/null || true
