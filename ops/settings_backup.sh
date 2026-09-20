#!/usr/bin/env bash
#
# Nightly dump of every driver's settings -- the one table whose loss is unrecoverable.
#
# decision_log rows survive a deletion (they are only de-identified) and offer_history has
# no driver column, but driver_settings_new is DELETED outright: line, cost per mile,
# markets, zones, the lot. On 2026-09-20 the app's author deleted his own account by
# mistake and it was recoverable only because an unrelated backup from the 15th happened to
# exist. This makes that boring.
#
# Keeps 30 days. One row per driver, id + settings json, gzipped: the whole table is tens of
# kilobytes.
set -uo pipefail

DIR=/home/andrew/backups/settings
LOG=/home/andrew/logs/settings_backup.log
mkdir -p "$DIR" "$(dirname "$LOG")"
STAMP=$(date -u +%Y%m%d)
OUT="$DIR/driver_settings_$STAMP.csv"

psql -h 10.128.0.3 -U postgres -d puddlejumper -Atc \
  "\\copy (select driver_id, settings from app_private.driver_settings_new order by driver_id) to '$OUT' with csv header" \
  >> "$LOG" 2>&1

if [ -s "$OUT" ]; then
    gzip -f "$OUT"
    ROWS=$(zcat "$OUT.gz" | wc -l)
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') ok $OUT.gz ($((ROWS - 1)) drivers)" >> "$LOG"
    # 30 days of history; older dumps go.
    find "$DIR" -name 'driver_settings_*.csv.gz' -mtime +30 -delete
else
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') FAILED: no rows written" >> "$LOG"
    rm -f "$OUT"
fi
tail -n 400 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
