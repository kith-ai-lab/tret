#!/usr/bin/env bash
#
# tret installer.
#
#   curl -fsSL https://tret.kithailab.com/install.sh | bash
#
# This file is the canonical copy. tret.kithailab.com serves it verbatim (the
# site's deploy fetches it from this repo), so the pretty URL above and
#   curl -fsSL https://raw.githubusercontent.com/voiz-academy/tret/main/install.sh | bash
# are the same script. Edit it here, nowhere else.
#
# What it does, in order: checks for Docker and git, clones (or updates)
# github.com/voiz-academy/tret into ~/kith-tret, creates a .env from the
# shipped example, builds and starts the containers, waits until tret answers,
# and opens it in your browser. Nothing is installed outside that one folder and
# Docker's own storage; `stop-tret.sh` in that folder shuts it all down again.
#
# Environment overrides:
#   TRET_DIR=/path/to/dir     where to install       (default ~/kith-tret)
#   TRET_REPO_URL=...         source repository
#   TRET_BRANCH=main          branch to check out
#   TRET_NO_START=1           clone/update only, don't build or start
#   TRET_NO_BROWSER=1         don't open a browser at the end
#
set -euo pipefail

REPO_URL="${TRET_REPO_URL:-https://github.com/voiz-academy/tret.git}"
BRANCH="${TRET_BRANCH:-main}"
INSTALL_DIR="${TRET_DIR:-$HOME/kith-tret}"
FRONTEND_URL="http://localhost:5180"
HEALTH_URL="http://localhost:8000/api/healthz"
DOCKER_DOWNLOAD_URL="https://www.docker.com/products/docker-desktop/"
STARTUP_TIMEOUT=900   # seconds to wait for the first build + boot

# ── Output helpers ───────────────────────────────────────────────────────────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; AMBER=$'\033[38;5;180m'
  RED=$'\033[31m'; GREEN=$'\033[32m'; RESET=$'\033[0m'
else
  BOLD=""; DIM=""; AMBER=""; RED=""; GREEN=""; RESET=""
fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s\n' "${AMBER}==>${RESET} ${BOLD}$*${RESET}"; }
note() { printf '%s\n' "    ${DIM}$*${RESET}"; }
ok()   { printf '%s\n' "    ${GREEN}✓${RESET} $*"; }
die()  { printf '\n%s\n' "${RED}✗ $*${RESET}" >&2; exit 1; }

open_in_browser() {
  if [ -n "${TRET_NO_BROWSER:-}" ]; then return 0; fi
  if command -v open >/dev/null 2>&1; then open "$1" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1 || true
  fi
}

compose() {
  if docker compose version >/dev/null 2>&1; then docker compose "$@"
  else docker-compose "$@"
  fi
}

say ""
say "${BOLD}tret${RESET} — an auditable AI workbench for knowledge work"
note "installing into $INSTALL_DIR"
say ""

# ── 1. Prerequisites ─────────────────────────────────────────────────────────
step "Checking prerequisites"

command -v git >/dev/null 2>&1 || die "git isn't installed.
    macOS:  xcode-select --install
    Linux:  sudo apt install git   (or your distribution's package manager)
    Then run this command again."
ok "git"

if ! command -v docker >/dev/null 2>&1; then
  open_in_browser "$DOCKER_DOWNLOAD_URL"
  die "Docker isn't installed — tret runs inside it.
    Install Docker Desktop from $DOCKER_DOWNLOAD_URL
    (the download page should have just opened), open it once, then run
    this command again."
fi

if ! docker info >/dev/null 2>&1; then
  note "Docker is installed but not running — trying to start it."
  if [ "$(uname -s)" = "Darwin" ]; then
    open -a Docker >/dev/null 2>&1 || open -a "Docker Desktop" >/dev/null 2>&1 || true
  fi
  printf '    waiting for Docker '
  waited=0
  until docker info >/dev/null 2>&1; do
    [ "$waited" -ge 120 ] && { printf '\n'; die "Docker didn't start within two minutes.
    Open Docker Desktop yourself, wait for its whale icon to settle,
    then run this command again."; }
    printf '.'; sleep 3; waited=$((waited + 3))
  done
  printf '\n'
fi
ok "Docker is running"

if ! docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then
  die "Docker is installed but Docker Compose isn't available.
    Docker Desktop includes it; on Linux install the docker-compose-plugin
    package, then run this command again."
fi
ok "Docker Compose"

# ── 2. Get the source ────────────────────────────────────────────────────────
say ""
if [ -d "$INSTALL_DIR/.git" ]; then
  step "Updating the existing install"
  cd "$INSTALL_DIR"
  if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    note "You have local changes in $INSTALL_DIR — leaving them alone and"
    note "skipping the update. Existing version will be started as-is."
  elif git pull --ff-only origin "$BRANCH" >/dev/null 2>&1; then
    ok "updated to the latest $BRANCH"
  else
    note "Couldn't fast-forward to the latest $BRANCH — starting what's there."
  fi
elif [ -e "$INSTALL_DIR" ]; then
  die "$INSTALL_DIR already exists and isn't a tret checkout.
    Move it aside, or install somewhere else:
      curl -fsSL https://tret.kithailab.com/install.sh | TRET_DIR=~/somewhere-else bash"
else
  step "Downloading tret"
  note "from $REPO_URL"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR" >/dev/null 2>&1 \
    || die "Download failed. Check your internet connection and try again."
  cd "$INSTALL_DIR"
  ok "downloaded to $INSTALL_DIR"
fi

# ── 3. First-run settings file ───────────────────────────────────────────────
if [ ! -f .env ]; then
  cp .env.example .env
  ok "created .env (you don't need to edit it — AI keys are added in the app)"
fi

if [ -n "${TRET_NO_START:-}" ]; then
  say ""
  say "Source is ready in $INSTALL_DIR (TRET_NO_START was set, so nothing was started)."
  exit 0
fi

# ── 4. Build and start ───────────────────────────────────────────────────────
say ""
step "Building and starting tret"
note "The first run downloads and builds everything — several minutes is normal."
note "Later starts take seconds."
say ""
compose up -d --build || die "tret failed to start. The messages above have the details.
    From $INSTALL_DIR, this shows more:  docker compose logs backend"

# ── 5. Wait for it to answer ─────────────────────────────────────────────────
health_ok() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS -o /dev/null --max-time 3 "$HEALTH_URL"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O /dev/null -T 3 "$HEALTH_URL"
  else
    sleep 15
  fi
}

say ""
printf '    waiting for tret to finish starting '
waited=0
until health_ok >/dev/null 2>&1; do
  if [ "$waited" -ge "$STARTUP_TIMEOUT" ]; then
    printf '\n'
    say ""
    say "tret hasn't answered yet. It may still be building — you can wait a"
    say "minute and open $FRONTEND_URL yourself, or look at what"
    say "it's doing with:"
    say "    cd $INSTALL_DIR && docker compose logs backend"
    exit 1
  fi
  printf '.'; sleep 3; waited=$((waited + 3))
done
printf '\n'
ok "tret is up"

# ── 6. Hand over ─────────────────────────────────────────────────────────────
open_in_browser "$FRONTEND_URL"

say ""
say "${AMBER}────────────────────────────────────────────────────────────${RESET}"
say "  ${BOLD}tret is running at $FRONTEND_URL${RESET}"
say ""
say "  Log in:      admin@example.com  /  tret-admin"
say "               (change these in $INSTALL_DIR/.env if"
say "               anyone else can reach this machine)"
say ""
say "  Next:        add an AI provider key on the Settings page —"
say "               one OpenRouter key is enough to try everything."
say ""
say "  Stop it:     cd $INSTALL_DIR && ./stop-tret.sh"
say "  Start again: cd $INSTALL_DIR && ./start-tret.sh"
say "  Update:      re-run this installer"
say "${AMBER}────────────────────────────────────────────────────────────${RESET}"
say ""
