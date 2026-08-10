#!/bin/bash
# Starts bench (http://localhost:5180) using Docker. Safe to run repeatedly.
# Double-click start-bench.command on a Mac; run ./start-bench.sh on Linux.
set -u

# Always work from the folder this script lives in, so double-clicking works
# no matter where the window was opened from.
cd "$(dirname "$0")" || exit 1

FRONTEND_URL="http://localhost:5180"
HEALTH_URL="http://localhost:8000/api/healthz"
DOCKER_DOWNLOAD_URL="https://www.docker.com/products/docker-desktop/"

say() { printf '%s\n' "$*"; }

open_in_browser() {
  if command -v open >/dev/null 2>&1; then open "$1"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1
  else say "Please open this address in your browser: $1"
  fi
}

# ── 1. Is Docker installed? ──────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  say ""
  say "bench runs inside Docker, and Docker doesn't seem to be installed yet."
  say "Opening the Docker Desktop download page in your browser now."
  say ""
  say "Install Docker Desktop, open it once, then double-click this file again."
  open_in_browser "$DOCKER_DOWNLOAD_URL"
  say ""
  read -r -p "Press Enter to close this window... " _ 2>/dev/null || true
  exit 1
fi

# Prefer "docker compose"; fall back to the older "docker-compose" if needed.
compose() {
  if docker compose version >/dev/null 2>&1; then docker compose "$@"
  else docker-compose "$@"
  fi
}

# ── 2. Is Docker running? If not, try to start it and wait. ─────────────────
if ! docker info >/dev/null 2>&1; then
  say "Docker is installed but not running yet."
  if [ "$(uname -s)" = "Darwin" ]; then
    say "Starting Docker Desktop for you (this can take a minute)..."
    open -a Docker 2>/dev/null || open -a "Docker Desktop" 2>/dev/null || true
  else
    say "Please start Docker (on most Linux systems: sudo systemctl start docker,"
    say "or open Docker Desktop if you use it). I'll wait here for it..."
  fi
  printf "Waiting for Docker to wake up "
  waited=0
  until docker info >/dev/null 2>&1; do
    if [ "$waited" -ge 120 ]; then
      say ""
      say ""
      say "Docker didn't start within 2 minutes."
      say "Open the Docker Desktop app yourself, wait for the whale icon to"
      say "settle, then double-click this file again."
      read -r -p "Press Enter to close this window... " _ 2>/dev/null || true
      exit 1
    fi
    printf "."
    sleep 3
    waited=$((waited + 3))
  done
  say " ready!"
fi

# ── 3. First run: create .env from the example. ─────────────────────────────
if [ ! -f .env ]; then
  cp .env.example .env
  say ""
  say "Created a settings file (.env) from the example that ships with bench."
  say "You don't need to edit it — AI provider keys are added later, inside"
  say "the app itself (Settings page), not in this file."
fi

# ── 4. Start everything. ────────────────────────────────────────────────────
say ""
say "Starting bench. The very first start downloads and builds everything,"
say "which can take several minutes — later starts take only seconds."
say ""
if ! compose up -d --build; then
  say ""
  say "Something went wrong while starting bench (the messages above have the"
  say "details). If you're stuck, run this from a terminal in this folder to"
  say "see more:  docker compose logs backend"
  read -r -p "Press Enter to close this window... " _ 2>/dev/null || true
  exit 1
fi

# ── 5. Wait until bench answers, then open the browser. ─────────────────────
health_ok() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS -o /dev/null --max-time 3 "$HEALTH_URL"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O /dev/null -T 3 "$HEALTH_URL"
  else
    # No way to check — assume it's coming up after a short grace period.
    sleep 15
  fi
}

say ""
printf "Waiting for bench to finish starting up "
waited=0
until health_ok >/dev/null 2>&1; do
  if [ "$waited" -ge 300 ]; then
    say ""
    say ""
    say "bench didn't come up within 5 minutes. It may still be building —"
    say "you can simply wait and then open $FRONTEND_URL yourself,"
    say "or see what it's doing with:  docker compose logs backend"
    read -r -p "Press Enter to close this window... " _ 2>/dev/null || true
    exit 1
  fi
  printf "."
  sleep 3
  waited=$((waited + 3))
done
say " it's up!"

say ""
say "Opening bench in your browser: $FRONTEND_URL"
open_in_browser "$FRONTEND_URL"

say ""
say "────────────────────────────────────────────────────────────"
say "  Log in with:   admin@example.com  /  bench-admin"
say "  (unless you've changed them in the .env file — do change"
say "  them if anyone else can reach this computer)."
say ""
say "  Add your AI provider key inside the app: Settings page."
say "  To stop bench later, double-click stop-bench."
say "────────────────────────────────────────────────────────────"
say ""
