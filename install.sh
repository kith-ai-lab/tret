#!/usr/bin/env bash
#
# tret installer.
#
#   curl -fsSL https://tret.kithailab.com/install.sh | bash
#
# This file is the canonical copy. tret.kithailab.com serves it verbatim (the
# site's deploy fetches it from this repo), so the pretty URL above and
#   curl -fsSL https://raw.githubusercontent.com/kith-ai-lab/tret/main/install.sh | bash
# are the same script. Edit it here, nowhere else.
#
# curl|bash runs this without you ever seeing it. If you'd rather read it
# first — reasonable, for anything that's about to touch your filesystem and
# start Docker containers — see README.md's "Install" section for the
# clone-then-run alternative: `git clone`, read this file, then `./install.sh`
# from the checkout. Same script either way.
#
# Or verify the piped download itself, without cloning: README.md's "Install"
# section also has a "verify before running" block that checks this exact
# file against install.sh.sha256 (also in this repo's root, and CI-checked
# against this file by backend/tests/test_install_checksum.py, so it can
# never silently drift) before you pipe it into bash. That CI check only
# guarantees the two files agree *in this repo* — install.sh.sha256 and the
# install.sh copy tret.kithailab.com serves must still be deployed together,
# in the same release, or the checksum published there would verify against
# a script version that isn't the one actually being served.
#
# What it does, in order: checks for Docker and git, clones (or updates)
# github.com/kith-ai-lab/tret into ~/kith-tret, creates a .env from the
# shipped example (with a freshly generated secret key and admin password), builds and starts the containers, waits until tret answers,
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

REPO_URL="${TRET_REPO_URL:-https://github.com/kith-ai-lab/tret.git}"
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

# ── First-run settings helpers ───────────────────────────────────────────────
# .env.example ships a placeholder secret key and admin password (the backend's
# production check recognises them). A fresh .env must never keep them: the
# stack is reachable by anyone who can reach this machine's ports.
rand_token() {  # rand_token <bytes> — URL-safe, no padding, no newline
  local out=""
  if command -v openssl >/dev/null 2>&1; then
    out=$(openssl rand -base64 "$1" 2>/dev/null | tr '+/' '-_' | tr -d '=\n') || out=""
  fi
  if [ -z "$out" ]; then
    out=$(head -c "$1" /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n') || out=""
  fi
  [ -n "$out" ] || return 1
  printf '%s' "$out"
}

env_value() {  # env_value <NAME> — last assignment wins; one pair of quotes stripped
  sed -n "s/^$1=//p" .env | tail -n 1 | tr -d '\r' \
    | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

env_default() {  # env_default <NAME> <fallback> — compose's :- also covers empty
  local v
  v=$(env_value "$1")
  printf '%s' "${v:-$2}"
}

# Is there a tret database volume from an earlier install? Compose names it
# <project>_pgdata; the project defaults to this folder's name, lowercased,
# with characters outside [a-z0-9_-] and leading _ or - dropped. Best effort: silent if docker
# isn't answering yet.
existing_db_volume() {
  local proj
  proj="${COMPOSE_PROJECT_NAME:-$(basename "$PWD" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9_-' | sed 's/^[_-]*//')}"
  [ -n "$proj" ] || return 1
  docker volume inspect "${proj}_pgdata" >/dev/null 2>&1
}

create_env_file() {
  local key pw
  EXISTING_DB=""
  if existing_db_volume; then EXISTING_DB=1; fi
  key=$(rand_token 32) && pw=$(rand_token 15) \
    || die "Couldn't generate random credentials (need openssl, or base64 and /dev/urandom)."
  (umask 077; awk -v key="$key" -v pw="$pw" '
    /^TRET_SECRET_KEY=/     { print "TRET_SECRET_KEY=" key; next }
    /^TRET_ADMIN_PASSWORD=/ { print "TRET_ADMIN_PASSWORD=" pw; next }
    { print }
  ' .env.example > .env.tmp) && mv .env.tmp .env \
    || die "Couldn't write .env."
}

warn_if_default_secrets() {
  local weak="" sk pw
  sk=$(env_value TRET_SECRET_KEY); pw=$(env_value TRET_ADMIN_PASSWORD)
  if [ -z "$sk" ]; then
    weak="secret key (TRET_SECRET_KEY is not set, so compose falls back to the default)"
  elif [ "$sk" = "dev-secret-change-me" ]; then weak="secret key"; fi
  if [ -z "$pw" ] || [ "$pw" = "tret-admin" ]; then weak="${weak:+$weak and }admin password"; fi
  [ -n "$weak" ] || return 0
  say ""
  say "${RED}!! $INSTALL_DIR/.env still has the published default $weak.${RESET}"
  say "   Anyone who can reach this machine can sign in as admin. Fix it:"
  say "     1. In .env set TRET_SECRET_KEY and TRET_ADMIN_PASSWORD to new random values."
  say "        (Changing the secret key makes AI provider keys saved in the app unreadable;"
  say "        re-enter them afterwards.)"
  say "     2. The admin account exists already, so also change its password in the app,"
  say "        then restart:  cd $INSTALL_DIR && ./stop-tret.sh && ./start-tret.sh"
}

warn_existing_db() {
  [ -n "${EXISTING_DB:-}" ] || return 0
  say ""
  say "${RED}!! An existing tret database was found from an earlier install.${RESET}"
  say "   It keeps its existing admin password (tret-admin if never changed), not the"
  say "   new one in .env. AI provider keys saved earlier were encrypted with the old"
  say "   secret key and must be re-entered in the app."
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
  create_env_file
  ok "created .env with a freshly generated secret key and admin password"
  note "AI keys are added in the app, not in this file"
  warn_existing_db
else
  warn_if_default_secrets
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
say "  Log in:      $(env_default TRET_ADMIN_EMAIL admin@example.com)"
if [ -n "${EXISTING_DB:-}" ]; then
  say "  Password:    unchanged — your existing database keeps its admin password"
  say "               (tret-admin if you never changed it). The new one in .env is not used."
else
  say "  Password:    $(env_default TRET_ADMIN_PASSWORD tret-admin)"
  say "               (kept as TRET_ADMIN_PASSWORD in $INSTALL_DIR/.env; it is used"
  say "               when the admin account is first created — to change it later,"
  say "               use the app)"
fi
say "  Reachable:   this computer only (localhost). Putting it on a network"
say "               takes TLS and a hardening pass — see docs/hardening.md."
say ""
say "  Next:        add an AI provider key on the Settings page —"
say "               one OpenRouter key is enough to try everything."
say ""
say "  Stop it:     cd $INSTALL_DIR && ./stop-tret.sh"
say "  Start again: cd $INSTALL_DIR && ./start-tret.sh"
say "  Update:      re-run this installer"
say "${AMBER}────────────────────────────────────────────────────────────${RESET}"
warn_existing_db
warn_if_default_secrets
say ""
