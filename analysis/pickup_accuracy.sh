#!/usr/bin/env bash
# THE mission metric, one command. Run on the VM after each drive:
#   ./analysis/pickup_accuracy.sh
# Pickup LOCATION accuracy (<100 m vs your manual taps) for the latest drive,
# with the miss breakdown by lever (band_reaped / never_queued / in_queue_no_fire).
# Optional arg: a different driver_id. Mission: >95% caught_100m.
set -euo pipefail

DRV="${1:-UjT1hE9eBXh2q95aSZYOkzDJ8lo1}"   # default: the real driver
PROJECT="puddle-jumper-477316"
export PGPASSWORD="${DB_PASSWORD:-$(gcloud secrets versions access latest --secret=DB_PASSWORD --project "$PROJECT")}"

psql -h 10.128.0.2 -U atjb -d puddlejumper -P pager=off \
     -v drv="'$DRV'" -f "$(dirname "$0")/pickup_accuracy.sql"
