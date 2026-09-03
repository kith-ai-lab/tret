#!/bin/sh
# Entrypoint wrapper for the `searxng` compose service (docker-compose.yml).
#
# The searxng/searxng image only ever generates its own secret_key when NO
# settings.yml exists yet at its config path (verified against the image's
# real /usr/local/searxng/entrypoint.sh) — templating one out and substituting
# a placeholder. This repo ships its own settings.yml (for the JSON API and
# limiter=false settings it needs, see ./settings.yml), so that generator
# never runs and the file's placeholder secret would otherwise be shipped
# into a running instance verbatim.
#
# This script runs in its place: it resolves a real secret (an operator's
# SEARXNG_SECRET if set, else one persisted on first boot so it survives
# restarts, else a freshly generated one), substitutes it into a copy of the
# source settings.yml at the image's actual config path, and only then hands
# off to the image's own entrypoint so the rest of its setup (ownership,
# certs) still runs normally.
set -eu

SRC_SETTINGS="/searxng-src/settings.yml"
DEST_SETTINGS="/etc/searxng/settings.yml"
SECRET_FILE="/run/searxng-secret/secret_key"

SECRET="${SEARXNG_SECRET:-}"
if [ -z "$SECRET" ]; then
    # 077: the persisted secret is as sensitive as the operator-supplied one
    # would be, and this directory/file must not be group- or world-readable
    # regardless of the image's own default umask. Restored below, before
    # DEST_SETTINGS is written, since that file has no such requirement (the
    # image's own entrypoint still needs to read/chown it normally).
    umask 077
    mkdir -p "$(dirname "$SECRET_FILE")"
    if [ -s "$SECRET_FILE" ]; then
        SECRET=$(cat "$SECRET_FILE")
    else
        SECRET=$(head -c 32 /dev/urandom | base64)
        printf '%s' "$SECRET" > "$SECRET_FILE"
    fi
    umask 022
fi

# Belt and suspenders against a secret that would otherwise corrupt the
# substitution below: an operator-supplied SEARXNG_SECRET is arbitrary text,
# and `&`, `#`, or `\` in it would be misread — by sed as a replacement
# escape/whole-match token regardless of delimiter choice, and (per POSIX)
# even by awk's own gsub() replacement string, which treats `&` the same
# way. Rather than rely on carefully avoiding every tool that reinterprets
# the replacement text, refuse anything outside the charset this file's
# config format and a generated (base64) secret can ever actually contain.
case "$SECRET" in
    ????????????????*) ;;
    *)
        echo "inject-secret-entrypoint: SEARXNG_SECRET must be at least 16 characters" >&2
        exit 1
        ;;
esac
case "$SECRET" in
    *[!A-Za-z0-9+/=_-]*)
        echo "inject-secret-entrypoint: SEARXNG_SECRET contains a character outside" \
             "[A-Za-z0-9+/=_-]; refusing to risk a silently corrupted secret_key" >&2
        exit 1
        ;;
esac

# Literal string replacement, not sed/gsub: walks the placeholder's own
# occurrences with index()/substr() and never passes $SECRET through
# anything that treats characters in it (`&`, backreferences) specially —
# this holds even independent of the charset validation above.
SECRET="$SECRET" PLACEHOLDER="change-me-searxng" awk '
    BEGIN { secret = ENVIRON["SECRET"]; placeholder = ENVIRON["PLACEHOLDER"]; plen = length(placeholder) }
    {
        line = $0
        out = ""
        while ((i = index(line, placeholder)) > 0) {
            out = out substr(line, 1, i - 1) secret
            line = substr(line, i + plen)
        }
        print out line
    }
' "$SRC_SETTINGS" > "$DEST_SETTINGS"

exec /usr/local/searxng/entrypoint.sh "$@"
