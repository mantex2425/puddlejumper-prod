#!/usr/bin/env bash
#
# Every 15 minutes, score a canned offer through the live service and shout if it fails.
#
# deploy.sh already smoke-tests a deploy; this catches everything that breaks between
# deploys -- a database change, an expired credential, a Cloud Run revision rolled back,
# a dependency that stops resolving. The failure mode it exists for is silent: the app
# looks healthy, captures the offer, and the driver simply never sees a verdict. On
# 2026-09-19 that lasted 20 minutes and was found by a driver on the road, not by us.
#
# Alerts go to Discord if a webhook is configured, and always to the log:
#   /home/andrew/logs/smoke_watch.log
# Two consecutive failures alert; one is usually a cold start losing a race with curl.
set -uo pipefail

LOG=/home/andrew/logs/smoke_watch.log
STATE=/home/andrew/logs/.smoke_watch_fails
URL="https://puddlejumper-api-152974241923.us-central1.run.app/internal/smoke/decision"
mkdir -p "$(dirname "$LOG")"

stamp() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

TOKEN=$(gcloud secrets versions access latest --secret=SMOKE_TOKEN 2>/dev/null | tr -d '\n')
if [ -z "$TOKEN" ]; then
    echo "$(stamp) CHECK-ERROR cannot read SMOKE_TOKEN" >> "$LOG"
    exit 0
fi

BODY=$(curl -s --max-time 45 -H "X-Smoke-Token: $TOKEN" "$URL")
FAILS=$(cat "$STATE" 2>/dev/null || echo 0)

case "$BODY" in
    *'"ok":true'*)
        [ "$FAILS" -gt 0 ] && echo "$(stamp) RECOVERED after $FAILS failure(s)" >> "$LOG"
        echo 0 > "$STATE"
        echo "$(stamp) ok $BODY" >> "$LOG"
        ;;
    *)
        FAILS=$((FAILS + 1))
        echo "$FAILS" > "$STATE"
        echo "$(stamp) FAIL($FAILS) $BODY" >> "$LOG"
        if [ "$FAILS" -eq 2 ]; then
            HOOK=$(grep -o 'DISCORD_WEBHOOK_URL=[^",]*' /home/andrew/puddlejumper-prod/deploy.sh 2>/dev/null | head -1 | cut -d= -f2-)
            REV=$(gcloud run services describe puddlejumper-api --region=us-central1 \
                  --format='value(status.latestReadyRevisionName)' 2>/dev/null)
            MSG="PuddleJumper is NOT scoring offers. Two checks in a row failed on revision ${REV:-unknown}. Response: ${BODY:0:300}"
            if [ -n "$HOOK" ]; then
                curl -s --max-time 20 -H 'Content-Type: application/json' \
                     -d "$(printf '{"content": %s}' "$(printf '%s' "$MSG" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')")" \
                     "$HOOK" > /dev/null
            fi
            echo "$(stamp) ALERTED: $MSG" >> "$LOG"
        fi
        ;;
esac

# keep the log to the last 2000 lines
tail -n 2000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
