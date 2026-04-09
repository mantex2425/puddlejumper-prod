#!/bin/bash
# test_integration.sh — PuddleJumper End-to-End Integration Test
# Tests full HTTP stack: decisions → heartbeat → state machine → DB
# Usage: bash test_integration.sh

set -e

SERVICE="https://puddlejumper-api-152974241923.us-central1.run.app"
DRIVER="UjT1hE9eBXh2q95aSZYOkzDJ8lo1"
DB="psql -h 10.128.0.2 -U postgres -d puddlejumper"
REPLAY='-H "X-Internal-Replay: puddlejumper-replay-2026" -H "X-Driver-Id: '"$DRIVER"'"'

PASS="✅ PASS"
FAIL="❌ FAIL"
FAILURES=0
TOTAL=0

# ── Reset test driver state before every run ──────────────────────────────────
$DB -c "
UPDATE app_private.driver_trip_state
SET state = 'UNCOMMITTED',
    pickup_lat = NULL, pickup_lng = NULL,
    dropoff_lat = NULL, dropoff_lng = NULL,
    nailed_pickup_lat = NULL, nailed_pickup_lng = NULL,
    nailed_pickup_error_m = NULL,
    nailed_dropoff_lat = NULL, nailed_dropoff_lng = NULL,
    nailed_dropoff_error_m = NULL,
    current_offer_id = NULL
WHERE driver_id = '$DRIVER';" > /dev/null 2>&1
echo "🔄 Test driver state reset to UNCOMMITTED"

# Houston test coordinates
PICKUP_LAT="29.76303018910312"
PICKUP_LNG="-95.37312533793694"
DROPOFF_LAT="29.71639028760345"
DROPOFF_LNG="-95.4012207103182"
DRIVER_LAT="29.7650"
DRIVER_LNG="-95.3720"

PICKUP2_LAT="29.7200"
PICKUP2_LNG="-95.3900"
DROPOFF2_LAT="29.7800"
DROPOFF2_LNG="-95.3800"

echo ""
echo "======================================================================"
echo "  🐸 PuddleJumper Integration Test Suite"
echo "  Service: $SERVICE"
echo "  Driver:  ${DRIVER:0:16}..."
echo "======================================================================"
echo ""

# ── Helpers ──────────────────────────────────────────────────────────────────

check() {
    local sid="$1"
    local desc="$2"
    local actual="$3"
    local expected="$4"
    TOTAL=$((TOTAL + 1))
    if [ "$actual" = "$expected" ]; then
        echo "  $PASS  $sid  $desc  ($actual)"
    else
        echo "  $FAIL  $sid  $desc  (got=$actual expected=$expected)"
        FAILURES=$((FAILURES + 1))
    fi
}

get_state() {
    $DB -t -c "SELECT state FROM app_private.driver_trip_state WHERE driver_id = '$DRIVER';" 2>/dev/null | tr -d ' '
}

get_log_count() {
    $DB -t -c "SELECT COUNT(*) FROM app_private.driver_trip_state_log WHERE driver_id = '$DRIVER' AND logged_at >= '${SCENARIO_START}'::timestamptz;" 2>/dev/null | tr -d ' '
}

reset_state() {
    $DB -q -c "SELECT * FROM app_private.sm_transition('$DRIVER', 'manual_reset', p_clear_coords := TRUE);" 2>/dev/null
}

clear_logs() {
    export SCENARIO_START=$($DB -t -c "SELECT NOW();" 2>/dev/null | xargs)
    $DB -q -c "DELETE FROM app_private.driver_trip_state_log WHERE driver_id = '$DRIVER' AND logged_at > NOW() - INTERVAL '1 hour';" 2>/dev/null
    $DB -q -c "DELETE FROM app_private.offer_history oh USING app_private.decision_log dl WHERE oh.decision_log_id = dl.id AND dl.driver_id = '$DRIVER' AND dl.created_at > NOW() - INTERVAL '1 hour';" 2>/dev/null
}

decide() {
    local fare="$1" miles="$2" minutes="$3" plat="$4" plng="$5" dlat="$6" dlng="$7"
    curl -s -X POST "$SERVICE/api/v1/decisions" \
        -H "X-Internal-Replay: puddlejumper-replay-2026" \
        -H "X-Driver-Id: $DRIVER" \
        -H "Content-Type: application/json" \
        -d "{
            \"fare\": $fare,
            \"tripMiles\": $miles,
            \"tripMinutes\": $minutes,
            \"pickupMinutes\": 3,
            \"pickupMiles\": 1.1,
            \"lat\": $plat,
            \"lng\": $plng,
            \"dropoffLat\": $dlat,
            \"dropoffLng\": $dlng,
            \"currentLat\": $DRIVER_LAT,
            \"currentLng\": $DRIVER_LNG,
            \"marketId\": \"6a35d28b-8e6c-4d60-94aa-2661e2650863\",
            \"marketName\": \"Houston\",
            \"isPuddleJumpMode\": false,
            \"towardsActive\": false
        }"
}

heartbeat() {
    local lat="$1" lng="$2" speed="$3" stopped="$4" dist="$5"
    curl -s -X POST "$SERVICE/api/v1/driver/heartbeat" \
        -H "X-Internal-Replay: puddlejumper-replay-2026" \
        -H "X-Driver-Id: $DRIVER" \
        -H "Content-Type: application/json" \
        -d "{
            \"armed\": true,
            \"target_type\": \"pickup\",
            \"dist_to_target_m\": $dist,
            \"cumulative_miles\": 0.0,
            \"stopped_seconds\": $stopped,
            \"required_stopped_seconds\": 10,
            \"speed_mph\": $speed,
            \"gps_accuracy_m\": 8,
            \"enroute_seconds\": 180,
            \"lat\": $lat,
            \"lng\": $lng
        }"
}

# ── SCENARIO 1: Full Solo Trip ────────────────────────────────────────────────

echo "── Scenario 1: Full Solo Trip ──────────────────────────────────────────"
echo ""
reset_state
clear_logs
sleep 1

# Step 1: Accept offer → ENROUTE
RESULT=$(decide 18.50 8.2 22 $PICKUP_LAT $PICKUP_LNG $DROPOFF_LAT $DROPOFF_LNG)
VERDICT=$(echo $RESULT | python3 -c "import sys,json; print(json.load(sys.stdin).get('verdict','?'))")
STATE=$(get_state)
check "T01" "Accept offer → ENROUTE" "$STATE" "ENROUTE"
check "T02" "Decision verdict = ACCEPT" "$VERDICT" "ACCEPT"

# Step 2: Heartbeat far from pickup → HOLD → stays ENROUTE
HB=$(heartbeat 29.7800 -95.3500 25.0 0 5000)
DS=$(echo $HB | python3 -c "import sys,json; print(json.load(sys.stdin).get('driverState','?'))")
STATE=$(get_state)
check "T03" "Heartbeat far from pickup → stays ENROUTE" "$STATE" "ENROUTE"

# Step 3: Heartbeat at pickup stopped → INITIAL_NAIL → IN_TRIP
HB=$(heartbeat $PICKUP_LAT $PICKUP_LNG 0.5 15 45)
DS=$(echo $HB | python3 -c "import sys,json; print(json.load(sys.stdin).get('driverState','?'))")
STATE=$(get_state)
check "T04" "Heartbeat at pickup stopped → IN_TRIP" "$STATE" "IN_TRIP"
check "T05" "driverState response = IN_TRIP" "$DS" "IN_TRIP"

# Step 4: Heartbeat driving away at speed → HOLD → stays IN_TRIP
HB=$(heartbeat 29.7450 -95.3850 35.0 0 3000)
DS=$(echo $HB | python3 -c "import sys,json; print(json.load(sys.stdin).get('driverState','?'))")
STATE=$(get_state)
check "T06" "Heartbeat driving to dropoff → stays IN_TRIP" "$STATE" "IN_TRIP"

# Step 5: Heartbeat at dropoff stopped → DROPOFF_NAIL → UNCOMMITTED
HB=$(heartbeat $DROPOFF_LAT $DROPOFF_LNG 0.5 15 45)
DS=$(echo $HB | python3 -c "import sys,json; print(json.load(sys.stdin).get('driverState','?'))")
STATE=$(get_state)
check "T07" "Heartbeat at dropoff stopped → UNCOMMITTED" "$STATE" "UNCOMMITTED"
check "T08" "driverState response = UNCOMMITTED" "$DS" "UNCOMMITTED"

# Step 6: Verify exactly 3 log entries
LOGS=$(get_log_count)
check "T09" "Exactly 3 state log entries for solo trip" "$LOGS" "3"

echo ""

# ── SCENARIO 2: Implicit Cancel (S04) ────────────────────────────────────────

echo "── Scenario 2: Implicit Cancel S04 ────────────────────────────────────"
echo ""
reset_state
clear_logs
sleep 1

# Accept first offer → ENROUTE
decide 18.50 8.2 22 $PICKUP_LAT $PICKUP_LNG $DROPOFF_LAT $DROPOFF_LNG > /dev/null
STATE=$(get_state)
check "T10" "First offer accepted → ENROUTE" "$STATE" "ENROUTE"

# New offer arrives while ENROUTE → S04 → UNCOMMITTED then ENROUTE
RESULT=$(decide 15.00 6.0 18 $PICKUP2_LAT $PICKUP2_LNG $DROPOFF2_LAT $DROPOFF2_LNG)
VERDICT=$(echo $RESULT | python3 -c "import sys,json; print(json.load(sys.stdin).get('verdict','?'))")
STATE=$(get_state)
check "T11" "New offer while ENROUTE → S04 implicit cancel" "$STATE" "ENROUTE"

echo ""

# ── SCENARIO 3: S11 Driver Override ──────────────────────────────────────────

echo "── Scenario 3: S11 Driver Override ────────────────────────────────────"
echo ""
reset_state
clear_logs
sleep 1

# Decline offer — $2.00 for 3mi/20min = ~$6/hr, well below $18 threshold
RESULT=$(decide 2.00 3.0 20 $PICKUP_LAT $PICKUP_LNG $DROPOFF_LAT $DROPOFF_LNG)
VERDICT=$(echo $RESULT | python3 -c "import sys,json; print(json.load(sys.stdin).get('verdict','?'))")
STATE=$(get_state)
check "T12" "Low fare offer → DECLINE → UNCOMMITTED" "$STATE" "UNCOMMITTED"

# Heartbeat at declined offer pickup → S11 → IN_TRIP
sleep 1
HB=$(heartbeat $PICKUP_LAT $PICKUP_LNG 0.5 15 45)
DS=$(echo $HB | python3 -c "import sys,json; print(json.load(sys.stdin).get('driverState','?'))")
STATE=$(get_state)
check "T13" "Heartbeat at declined pickup → S11 → IN_TRIP" "$STATE" "IN_TRIP"

echo ""

# ── SCENARIO 4: Manual Reset ──────────────────────────────────────────────────

echo "── Scenario 4: Manual Reset ────────────────────────────────────────────"
echo ""
reset_state
clear_logs
sleep 1

# Accept offer → ENROUTE
decide 18.50 8.2 22 $PICKUP_LAT $PICKUP_LNG $DROPOFF_LAT $DROPOFF_LNG > /dev/null
# Nail pickup → IN_TRIP
heartbeat $PICKUP_LAT $PICKUP_LNG 0.5 15 45 > /dev/null
STATE=$(get_state)
check "T14" "Setup: reached IN_TRIP" "$STATE" "IN_TRIP"

# Manual reset
RESET=$(curl -s -X POST "$SERVICE/api/v1/driver/reset-state" \
    -H "X-Internal-Replay: puddlejumper-replay-2026" \
    -H "X-Driver-Id: $DRIVER" \
    -H "Content-Type: application/json")
STATE=$(get_state)
check "T15" "Manual reset → UNCOMMITTED" "$STATE" "UNCOMMITTED"

echo ""

# ── SCENARIO 5: ABORT Guard (pickup nailed = no abort) ───────────────────────

echo "── Scenario 5: ABORT Guard ─────────────────────────────────────────────"
echo ""
reset_state
clear_logs
sleep 1

# Accept → ENROUTE → nail pickup → IN_TRIP
decide 18.50 8.2 22 $PICKUP_LAT $PICKUP_LNG $DROPOFF_LAT $DROPOFF_LNG > /dev/null
heartbeat $PICKUP_LAT $PICKUP_LNG 0.5 15 45 > /dev/null
STATE=$(get_state)
check "T16" "Setup: IN_TRIP with nailed pickup" "$STATE" "IN_TRIP"

# Backdate state so grace period satisfied — disable timestamp trigger first
psql -h 10.128.0.2 -U postgres -d puddlejumper -q << 'SQLEOF'
ALTER TABLE app_private.driver_trip_state DISABLE TRIGGER tr_set_state_timestamp;
UPDATE app_private.driver_trip_state SET state_updated_at = NOW() AT TIME ZONE 'America/Chicago' - INTERVAL '90 seconds' WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1';
ALTER TABLE app_private.driver_trip_state ENABLE TRIGGER tr_set_state_timestamp;
SQLEOF

# Heartbeat far away at speed → ABORT guard should block → stays IN_TRIP
HB=$(heartbeat 29.7900 -95.3200 45.0 0 8000)
DS=$(echo $HB | python3 -c "import sys,json; print(json.load(sys.stdin).get('driverState','?'))")
STATE=$(get_state)
check "T17" "Driving at speed after pickup nail → stays IN_TRIP (ABORT blocked)" "$STATE" "IN_TRIP"

echo ""

# ── Summary ───────────────────────────────────────────────────────────────────

echo "────────────────────────────────────────────────────────────────────────"
PASSED=$((TOTAL - FAILURES))
if [ $FAILURES -eq 0 ]; then
    echo ""
    echo "  ✅  $PASSED/$TOTAL passed | 0 failed — READY TO DRIVE 🐸"
else
    echo ""
    echo "  ❌  $PASSED/$TOTAL passed | $FAILURES failed — DO NOT DRIVE"
fi
echo ""
echo "======================================================================"
echo ""

# ── SCENARIO 6: Stacked Trip ──────────────────────────────────────────────────

echo "── Scenario 6: Full Stacked Trip ──────────────────────────────────────"
echo ""
reset_state
clear_logs
sleep 1

# Accept first offer → ENROUTE
decide 18.50 8.2 22 $PICKUP_LAT $PICKUP_LNG $DROPOFF_LAT $DROPOFF_LNG > /dev/null
# Nail pickup → IN_TRIP
heartbeat $PICKUP_LAT $PICKUP_LNG 0.5 15 45 > /dev/null
STATE=$(get_state)
check "T18" "Stacked setup: IN_TRIP after pickup nail" "$STATE" "IN_TRIP"

# Accept stack offer while IN_TRIP → STACKED
RESULT=$(decide 18.50 7.0 20 $PICKUP2_LAT $PICKUP2_LNG $DROPOFF2_LAT $DROPOFF2_LNG)
VERDICT=$(echo $RESULT | python3 -c "import sys,json; print(json.load(sys.stdin).get('verdict','?'))")
STATE=$(get_state)
check "T19" "Stack offer accepted → STACKED" "$STATE" "STACKED"

# Heartbeat at primary dropoff → buffer swap → ENROUTE
HB=$(heartbeat $DROPOFF_LAT $DROPOFF_LNG 0.5 15 45)
DS=$(echo $HB | python3 -c "import sys,json; print(json.load(sys.stdin).get('driverState','?'))")
STATE=$(get_state)
check "T20" "Heartbeat at primary dropoff → ENROUTE (buffer swap)" "$STATE" "ENROUTE"

# Nail secondary pickup → IN_TRIP
HB=$(heartbeat $PICKUP2_LAT $PICKUP2_LNG 0.5 15 45)
DS=$(echo $HB | python3 -c "import sys,json; print(json.load(sys.stdin).get('driverState','?'))")
STATE=$(get_state)
check "T21" "Heartbeat at secondary pickup → IN_TRIP" "$STATE" "IN_TRIP"

# Nail secondary dropoff → UNCOMMITTED
HB=$(heartbeat $DROPOFF2_LAT $DROPOFF2_LNG 0.5 15 45)
DS=$(echo $HB | python3 -c "import sys,json; print(json.load(sys.stdin).get('driverState','?'))")
STATE=$(get_state)
check "T22" "Heartbeat at secondary dropoff → UNCOMMITTED" "$STATE" "UNCOMMITTED"

# Verify 5 log entries for stacked trip
LOGS=$(get_log_count)
check "T23" "Exactly 6 state log entries for stacked trip" "$LOGS" "6"

echo ""

# ── SCENARIO 7: GPS Jitter at Pickup ─────────────────────────────────────────

echo "── Scenario 7: GPS Jitter at Pickup ───────────────────────────────────"
echo ""
reset_state
clear_logs
sleep 1

decide 18.50 8.2 22 $PICKUP_LAT $PICKUP_LNG $DROPOFF_LAT $DROPOFF_LNG > /dev/null

# Heartbeat 2000m away — should HOLD (INITIAL_NAIL_RADIUS_M=1000m)
HB=$(heartbeat 29.7480 -95.3600 5.0 0 2000)
STATE=$(get_state)
check "T24" "GPS 500m from pickup → HOLD → stays ENROUTE" "$STATE" "ENROUTE"

# Heartbeat 1500m away moving — should HOLD
HB=$(heartbeat 29.7530 -95.3650 8.0 0 1500)
STATE=$(get_state)
check "T25" "GPS 200m from pickup moving → HOLD → stays ENROUTE" "$STATE" "ENROUTE"

# Heartbeat at pickup stopped — INITIAL_NAIL → IN_TRIP
HB=$(heartbeat $PICKUP_LAT $PICKUP_LNG 0.5 15 45)
STATE=$(get_state)
check "T26" "GPS at pickup stopped → INITIAL_NAIL → IN_TRIP" "$STATE" "IN_TRIP"

echo ""

# ── SCENARIO 8: Duplicate Offer (S26) ────────────────────────────────────────

echo "── Scenario 8: Duplicate Offer S26 ────────────────────────────────────"
echo ""
reset_state
clear_logs
sleep 1

# First offer → ENROUTE
decide 18.50 8.2 22 $PICKUP_LAT $PICKUP_LNG $DROPOFF_LAT $DROPOFF_LNG > /dev/null
STATE=$(get_state)
check "T27" "First offer → ENROUTE" "$STATE" "ENROUTE"

# Same offer again (network retry) → should stay ENROUTE, not double-transition
decide 18.50 8.2 22 $PICKUP_LAT $PICKUP_LNG $DROPOFF_LAT $DROPOFF_LNG > /dev/null
STATE=$(get_state)
check "T28" "Duplicate offer → stays ENROUTE (not double-accepted)" "$STATE" "ENROUTE"

LOGS=$(get_log_count)
check "T29" "Duplicate offer → still exactly 1 log entry" "$LOGS" "1"

echo ""

# ── SCENARIO 9: Watchdog Auto-Reset ──────────────────────────────────────────

echo "── Scenario 9: Watchdog Auto-Reset ────────────────────────────────────"
echo ""
reset_state
clear_logs
sleep 1

# Accept → nail pickup → IN_TRIP
decide 18.50 8.2 22 $PICKUP_LAT $PICKUP_LNG $DROPOFF_LAT $DROPOFF_LNG > /dev/null
heartbeat $PICKUP_LAT $PICKUP_LNG 0.5 15 45 > /dev/null
STATE=$(get_state)
check "T30" "Watchdog setup: IN_TRIP" "$STATE" "IN_TRIP"

# Backdate state 95 minutes — disable timestamp trigger to prevent overwrite
psql -h 10.128.0.2 -U postgres -d puddlejumper -q -c "ALTER TABLE app_private.driver_trip_state DISABLE TRIGGER tr_set_state_timestamp;" 2>/dev/null
psql -h 10.128.0.2 -U postgres -d puddlejumper -q -c "UPDATE app_private.driver_trip_state SET state_updated_at = NOW() AT TIME ZONE 'America/Chicago' - INTERVAL '95 minutes' WHERE driver_id = '$DRIVER';" 2>/dev/null
psql -h 10.128.0.2 -U postgres -d puddlejumper -q -c "ALTER TABLE app_private.driver_trip_state ENABLE TRIGGER tr_set_state_timestamp;" 2>/dev/null

# Trigger watchdog — no auth needed, internal endpoint
curl -s -X POST "$SERVICE/internal/monitor" > /dev/null
sleep 3

STATE=$(get_state)
check "T31" "Watchdog fires after 95min IN_TRIP → UNCOMMITTED" "$STATE" "UNCOMMITTED"

echo ""

# ── SCENARIO 10: No-Show Resolution ──────────────────────────────────────────

echo "── Scenario 10: No-Show Resolution ────────────────────────────────────"
echo ""
reset_state
clear_logs
sleep 1

# Accept primary → nail pickup → IN_TRIP (rider no-shows)
decide 18.50 8.2 22 $PICKUP_LAT $PICKUP_LNG $DROPOFF_LAT $DROPOFF_LNG > /dev/null
heartbeat $PICKUP_LAT $PICKUP_LNG 0.5 15 45 > /dev/null
STATE=$(get_state)
check "T32" "No-show setup: IN_TRIP after pickup nail" "$STATE" "IN_TRIP"

# New offer appears (Uber presents next ride) → STACKED
decide 18.50 7.0 20 $PICKUP2_LAT $PICKUP2_LNG $DROPOFF2_LAT $DROPOFF2_LNG > /dev/null
STATE=$(get_state)
check "T33" "No-show: new offer accepted → STACKED" "$STATE" "STACKED"

# Driver heads to secondary pickup
# STACKED + GPS at primary dropoff area → S17 buffer swap → ENROUTE
HB=$(heartbeat $DROPOFF_LAT $DROPOFF_LNG 0.5 15 45)
STATE=$(get_state)
check "T34" "No-show: GPS at primary dropoff → ENROUTE (buffer swap)" "$STATE" "ENROUTE"

# Now nail secondary pickup → IN_TRIP
HB=$(heartbeat $PICKUP2_LAT $PICKUP2_LNG 0.5 15 45)
STATE=$(get_state)
check "T34b" "No-show: GPS at secondary pickup → IN_TRIP" "$STATE" "IN_TRIP"

# Complete the real ride
HB=$(heartbeat $DROPOFF2_LAT $DROPOFF2_LNG 0.5 15 45)
STATE=$(get_state)
check "T35" "No-show: complete secondary ride → UNCOMMITTED" "$STATE" "UNCOMMITTED"

echo ""

# ── SCENARIO 11: Timing Stress — Rapid Heartbeats ────────────────────────────

echo "── Scenario 11: Timing Stress ──────────────────────────────────────────"
echo ""
reset_state
clear_logs
sleep 1

decide 18.50 8.2 22 $PICKUP_LAT $PICKUP_LNG $DROPOFF_LAT $DROPOFF_LNG > /dev/null

# Fire 5 rapid heartbeats at pickup simultaneously
for i in {1..5}; do
    heartbeat $PICKUP_LAT $PICKUP_LNG 0.5 15 45 > /dev/null &
done
wait
sleep 2

STATE=$(get_state)
check "T36" "5 rapid heartbeats at pickup → IN_TRIP (not UNCOMMITTED)" "$STATE" "IN_TRIP"

# Count INITIAL_NAIL entries — should be exactly 1
NAIL_COUNT=$(psql -h 10.128.0.2 -U postgres -d puddlejumper -t \
    -c "SELECT COUNT(*) FROM app_private.driver_trip_state_log WHERE driver_id = '$DRIVER' AND trigger_event = 'gps_convergence' AND from_state = 'ENROUTE' AND logged_at >= '${SCENARIO_START}'::timestamptz;" 2>/dev/null | tr -d ' ')
check "T37" "Exactly 1 INITIAL_NAIL from rapid heartbeats (no double-fire)" "$NAIL_COUNT" "1"

echo ""

# ── SCENARIO 12: ABORT fires when pickup NOT nailed ──────────────────────────

echo "── Scenario 12: ABORT fires without pickup nail ────────────────────────"
echo ""
reset_state
clear_logs
sleep 1

decide 18.50 8.2 22 $PICKUP_LAT $PICKUP_LNG $DROPOFF_LAT $DROPOFF_LNG > /dev/null
STATE=$(get_state)
check "T38" "ABORT setup: ENROUTE, pickup not nailed" "$STATE" "ENROUTE"

# Backdate state 90s so grace period satisfied — disable timestamp trigger first
psql -h 10.128.0.2 -U postgres -d puddlejumper -q << 'SQLEOF'
ALTER TABLE app_private.driver_trip_state DISABLE TRIGGER tr_set_state_timestamp;
UPDATE app_private.driver_trip_state SET state_updated_at = NOW() AT TIME ZONE 'America/Chicago' - INTERVAL '90 seconds' WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1';
ALTER TABLE app_private.driver_trip_state ENABLE TRIGGER tr_set_state_timestamp;
SQLEOF

# Heartbeat far away at speed — ABORT should fire
HB=$(heartbeat 29.7900 -95.3200 45.0 0 8000)
DS=$(echo $HB | python3 -c "import sys,json; print(json.load(sys.stdin).get('driverState','?'))")
STATE=$(get_state)
check "T39" "Diverging at speed without pickup nail → ABORT → UNCOMMITTED" "$STATE" "UNCOMMITTED"

echo ""

# ── SCENARIO 13: Manual Reset from Each State ────────────────────────────────

echo "── Scenario 13: Manual Reset from Each State ───────────────────────────"
echo ""

# Reset from ENROUTE
reset_state; clear_logs; sleep 1
decide 18.50 8.2 22 $PICKUP_LAT $PICKUP_LNG $DROPOFF_LAT $DROPOFF_LNG > /dev/null
curl -s -X POST "$SERVICE/api/v1/driver/reset-state" \
    -H "X-Internal-Replay: puddlejumper-replay-2026" \
    -H "X-Driver-Id: $DRIVER" \
    -H "Content-Type: application/json" > /dev/null
STATE=$(get_state)
check "T40" "Manual reset from ENROUTE → UNCOMMITTED" "$STATE" "UNCOMMITTED"

# Reset from STACKED
reset_state; clear_logs; sleep 1
decide 18.50 8.2 22 $PICKUP_LAT $PICKUP_LNG $DROPOFF_LAT $DROPOFF_LNG > /dev/null
heartbeat $PICKUP_LAT $PICKUP_LNG 0.5 15 45 > /dev/null
decide 18.50 7.0 20 $PICKUP2_LAT $PICKUP2_LNG $DROPOFF2_LAT $DROPOFF2_LNG > /dev/null
curl -s -X POST "$SERVICE/api/v1/driver/reset-state" \
    -H "X-Internal-Replay: puddlejumper-replay-2026" \
    -H "X-Driver-Id: $DRIVER" \
    -H "Content-Type: application/json" > /dev/null
STATE=$(get_state)
check "T41" "Manual reset from STACKED → UNCOMMITTED" "$STATE" "UNCOMMITTED"

# Verify coords cleared after reset
COORDS=$(psql -h 10.128.0.2 -U postgres -d puddlejumper -t \
    -c "SELECT pickup_lat FROM app_private.driver_trip_state WHERE driver_id = '$DRIVER';" 2>/dev/null | tr -d ' ')
check "T42" "Coords cleared after manual reset" "$COORDS" ""

echo ""

# ── Updated Summary ───────────────────────────────────────────────────────────

echo "────────────────────────────────────────────────────────────────────────"
PASSED=$((TOTAL - FAILURES))
if [ $FAILURES -eq 0 ]; then
    echo ""
    echo "  ✅  $PASSED/$TOTAL passed | 0 failed — READY TO DRIVE 🐸"
else
    echo ""
    echo "  ❌  $PASSED/$TOTAL passed | $FAILURES failed — DO NOT DRIVE"
fi
echo ""
echo "======================================================================"
echo ""
