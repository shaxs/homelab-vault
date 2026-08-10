#!/bin/sh
# Homelab Vault — external watchdog (v1 spec §5a, §11.5).
#
# WHAT THIS IS FOR
#     Homelab Vault detects a dead *worker* by itself: each worker beats a
#     heartbeat, and going quiet for three poll intervals is a stall. But that
#     check only runs when something asks — `deadman.evaluate()` is called from
#     the `/api/v1/healthz` request handler and from `manage.py deadman`, and
#     nothing schedules either on its own. An alert nobody evaluates never fires.
#
#     And no amount of internal machinery can report the whole container being
#     down. That needs something outside it. This is that something.
#
#     Polling /healthz does BOTH jobs at once:
#       * the read drives the internal evaluation, so a stalled worker gets its
#         own Discord message from the app, with the app's own wording;
#       * if this script cannot reach the app at all, it says so itself — the one
#         failure the app is structurally unable to announce.
#
#     Which is why this curls the endpoint rather than running
#     `docker compose exec web python manage.py deadman`: that variant evaluates
#     the workers but is itself blind to the stack being down, because a failed
#     `exec` looks like nothing happening.
#
# INSTALL
#     Runs on the HOST, not in a container — a watchdog inside the thing it
#     watches is not a watchdog. Every minute:
#
#       * * * * * /path/to/homelab-vault/ops/watchdog.sh >> /var/log/hlv-watchdog.log 2>&1
#
#     Nothing here needs Docker, the database, or the repo's virtualenv: just
#     `curl`. It reads the webhook from the environment, or from the `.env` beside
#     the repo if that is where you keep it.
#
# CONFIGURE (all optional; the defaults suit a single-host compose install)
#     HLV_HEALTHZ_URL   default http://localhost:8000/api/v1/healthz
#     HLV_STATE_FILE    default /tmp/hlv-watchdog.state
#     HLV_TIMEOUT_S     default 10
#     HOMELABVAULT_DISCORD_WEBHOOK_URL
#                       the webhook. Read from the environment first, then from
#                       `.env` next to this script's repo.
#
# EXIT CODES
#     0  the app answered 200 (everything healthy)
#     1  the app answered 503 (a worker is stalled — the APP has alerted, not us)
#     2  the app could not be reached (WE alerted)
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

URL="${HLV_HEALTHZ_URL:-http://localhost:8000/api/v1/healthz}"
STATE_FILE="${HLV_STATE_FILE:-/tmp/hlv-watchdog.state}"
TIMEOUT="${HLV_TIMEOUT_S:-10}"

# The webhook, from the environment or from `.env`. Never echoed — a Discord
# webhook's secret is its URL, so printing it in a log is the same mistake as
# printing a password. (The app itself had exactly this bug via httpx's request
# logging, found 2026-08-07.)
WEBHOOK="${HOMELABVAULT_DISCORD_WEBHOOK_URL:-}"
if [ -z "$WEBHOOK" ] && [ -r "$REPO_DIR/.env" ]; then
    WEBHOOK=$(sed -n 's/^HOMELABVAULT_DISCORD_WEBHOOK_URL=//p' "$REPO_DIR/.env" | tail -n 1)
fi

stamp() { date '+%Y-%m-%d %H:%M:%S'; }

# Post to Discord. `allowed_mentions.parse: []` for the same reason the app sets
# it: nothing in a message should be able to ping the whole server.
notify() {
    if [ -z "$WEBHOOK" ]; then
        echo "$(stamp) WARN  no webhook configured; cannot alert"
        return 0
    fi
    curl -fsS --max-time "$TIMEOUT" -X POST "$WEBHOOK" \
        -H 'Content-Type: application/json' \
        -d "{\"content\":$1,\"allowed_mentions\":{\"parse\":[]}}" \
        >/dev/null 2>&1 \
        || echo "$(stamp) WARN  could not post to the webhook in HOMELABVAULT_DISCORD_WEBHOOK_URL"
}

previous="ok"
[ -r "$STATE_FILE" ] && previous=$(cat "$STATE_FILE" 2>/dev/null || echo ok)

remember() {
    # A failed write must not make every run alert again, but it must be visible.
    printf '%s' "$1" > "$STATE_FILE" 2>/dev/null \
        || echo "$(stamp) WARN  could not write $STATE_FILE; alerts may repeat"
}

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time "$TIMEOUT" "$URL" 2>/dev/null) || code="000"

case "$code" in
    200)
        # Healthy. Announce recovery only if we were the one who said it was down
        # — a stalled worker's recovery is the app's message to send, not ours.
        if [ "$previous" = "unreachable" ]; then
            notify '"✅ Homelab Vault is answering again."'
            echo "$(stamp) INFO  recovered (200)"
        fi
        remember ok
        exit 0
        ;;
    503)
        # A worker is stalled. **We deliberately do not post here**: this very
        # request drove the app's own evaluation, and the app has already sent a
        # message naming the worker and its threshold. Two alerts for one event
        # trains people to ignore both.
        echo "$(stamp) INFO  503 — a worker is stalled; the app has alerted"
        remember ok
        exit 1
        ;;
    *)
        # Includes 000 (connection refused, DNS failure, timeout) and any 5xx
        # that is not 503 — the app is not answering, which is the failure it
        # cannot report about itself.
        if [ "$previous" != "unreachable" ]; then
            notify "\"🚨 Homelab Vault is not answering (HTTP ${code}). The catalog, the API and both workers may be down.\""
            echo "$(stamp) ERROR unreachable (HTTP $code) — alerted"
        else
            echo "$(stamp) ERROR still unreachable (HTTP $code) — already alerted"
        fi
        remember unreachable
        exit 2
        ;;
esac
