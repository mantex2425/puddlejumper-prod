════════════════════════════════════════════════════════════════
  PUDDLEJUMPER — CANONICAL SESSION BRIEF
  Last updated: April 6, 2026
  Current deploy: puddlejumper-api-00484-rct
  Test suite: 47/47 unit + 43/43 integration passing
════════════════════════════════════════════════════════════════

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  GOLDEN RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  "Postgres owns the Truth. Python owns the Strategy."

  The state machine is a hierarchical 4-box controller.
  Every piece of logic has an address:
      [State Level] → [Monitor|Diagnose|Plan|Execute]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  THE STATE MACHINE — HIERARCHICAL 4-BOX CONTROLLER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Each driver state is a controller level with two independent
Monitor feeds triggering the same Diagnose → Plan → Execute loop.

  MONITOR FEED 1: Offer card (Android accessibility service)
  MONITOR FEED 2: GPS heartbeat (every 5 seconds)

UNCOMMITTED — idle, waiting for work
  Monitor 1 (offer):   Score offer → accept → ENROUTE
  Monitor 2 (GPS):     S11 scan → driver at declined pickup
                       → IN_TRIP

ENROUTE — navigating to pickup
  Monitor 1 (offer):   New offer proves primary cancelled (S04)
                       → UNCOMMITTED → re-evaluate new offer
  Monitor 2 (GPS):     At pickup → INITIAL_NAIL → IN_TRIP
                       Diverging at speed + pickup NOT nailed
                       → ABORT → UNCOMMITTED (driver left)
                       Diverging at speed + pickup WAS nailed
                       → HOLD (passenger aboard, normal driving
                         — NEVER abort)

IN_TRIP — ride in progress
  Monitor 1 (offer):   Stack opportunity → score → STACKED
  Monitor 2 (GPS):     At dropoff → DROPOFF_NAIL → UNCOMMITTED
                       S12: at unexpected pickup → IN_TRIP
                       (primary cancelled, secondary promoted)

STACKED — managing two rides
  Monitor 1 (offer):   3rd offer → one ride died → flag
                       potential_cancellation → stay STACKED
  Monitor 2 (GPS):     At primary dropoff → buffer swap → ENROUTE
                       S12 at secondary pickup → IN_TRIP
                       S07 pickup_confirmed → IN_TRIP

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  THE FOUR BOXES — WHERE CODE BELONGS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MONITOR   — sensor inputs only. No logic. No writes.
            Offer card (Android) + GPS heartbeat (driver_heartbeat.py)
            Rule: if it writes state, it is not a Monitor.

DIAGNOSE  — interpret sensor data. Pure reads. No writes.
            decision_engine_v2, check_convergence(),
            sm_read(), sm_find_nearby_offer()
            Rule: if it writes state, it is not a Diagnose.

PLAN      — decide next action. Business logic. No DB writes.
            Threshold math, market rate comparison,
            convergence verdict, S11/S12/S17 logic
            Rule: if it touches the DB, it is not a Plan.

EXECUTE   — one write path only. No business logic.
            DriverStateMachine.transition() → sm_transition()
            Rule: this is the ONLY place state changes.

Before adding any code, ask:
  1. Which state level does this belong to?
  2. Which Monitor feed triggers it?
  3. Is it Diagnose (read), Plan (logic), or Execute (write)?
  4. If Execute — does it go through sm_transition()?
     If not — it does not belong here.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ENFORCEMENT LAYERS (three gates, every transition)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  1. Python wrapper    rejects unknown triggers early
  2. sm_transition()   validates against valid_state_transitions
  3. DB trigger        enforce_state_transition_trigger, final gate

Nothing reaches the database without passing all three.
SET LOCAL app.state_trigger is handled by sm_transition()
internally — never call it directly from Python.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  IMPLICIT CANCELLATION — GPS IS ALWAYS THE TRUTH
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Uber never sends an explicit cancellation signal.
Cancellations are detected through Monitor feeds only:

  OFFER CARD (Monitor 1):
    New offer while ENROUTE → primary cancelled (S04)
    New offer while IN_TRIP → possible stack or no-show

  GPS TRUTH (Monitor 2):
    Driver arrives at different pickup → S11/S12 promote
    Driver arrives at dropoff → ride completed
    Driver diverges without nailing pickup → ABORT

No-show resolution:
    Primary no-show → new offer accepted → STACKED
    Driver heads to secondary pickup → S12 promotes → IN_TRIP
    Ghost primary forgotten — GPS truth wins

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ABORT GUARD — CRITICAL RULE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ABORT only fires when ALL of these are true:
  - state = ENROUTE
  - nailed_pickup_lat IS NULL  ← pickup never confirmed
  - dist_m > 1200m from pickup
  - speed_mph > 25
  - state_seconds > 60 (grace period for U-turns)

If nailed_pickup_lat IS NOT NULL:
  - Driver physically confirmed pickup location
  - Diverging at speed = normal driving to dropoff
  - ABORT is NEVER fired — verdict = HOLD
  - State machine waits for DROPOFF_NAIL

Without this guard, picking up a passenger triggers ABORT.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  CANONICAL RULES — NEVER VIOLATE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STATE MACHINE:
  DriverStateMachine.transition() is the ONLY write path
  DriverStateMachine.read() — pure read, no side effects
  Never call SET LOCAL app.state_trigger directly
  Never write to driver_trip_state with raw SQL
  Never disable enforce_state_transition_trigger

ABORT:
  Never fire if nailed_pickup_lat IS NOT NULL

COORDINATES — always use canonical functions:
  app_private.distance_miles(lat1, lng1, lat2, lng2)
  app_private.safe_h3(lat, lng)
  app_private.coords_to_h3(lat, lng)
  app_private.coords_to_point(lat, lng)
  app_private.coords_to_geography(lat, lng)
  app_private.h3_to_lat(h3), app_private.h3_to_lng(h3)
  Args always (lat, lng) — NEVER reversed
  NEVER: ST_MakePoint, h3_latlng_to_cell, h3_cell_to_latlng

TIME:
  ALWAYS: NOW() AT TIME ZONE 'America/Chicago'
  NEVER: bare NOW(), UTC offsets

TESTING:
  47/47 must pass before every deploy
  python3 ~/puddlejumper-prod/test_state_machine.py  # 47/47
  bash ~/puddlejumper-prod/test_integration.sh        # 43/43

DEPLOY:
  bash deploy.sh from ~/puddlejumper-prod/

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  WHAT STAYS IN PYTHON — NEVER BECOMES A STORED PROC
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Decision engine threshold math    changes frequently
  Triangulation / arc-banding       requires Google Maps API
  YOLO/OCR pipeline                 Android-side
  Discord notifications             Postgres has no HTTP
  check_convergence()               pure Python math, no DB writes
  Firebase auth                     external service

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  PRE-DRIVE CHECKLIST
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  # 1. Clear Android logs
  adb shell rm /sdcard/Download/puddle_logcat.txt

  # 2. Run test suite — must be 47/47
  python3 ~/puddlejumper-prod/test_state_machine.py  # 47/47
  bash ~/puddlejumper-prod/test_integration.sh        # 43/43

  # 3. Confirm active revision
  gcloud run revisions list --service=puddlejumper-api \
    --region=us-central1 --limit=1

  # 4. Reset driver state
  psql -h 10.128.0.2 -U postgres -d puddlejumper -c "SELECT * FROM app_private.sm_transition('UjT1hE9eBXh2q95aSZYOkzDJ8lo1', 'manual_reset', p_clear_coords := TRUE);"

  # 5. Confirm clean state
  psql -h 10.128.0.2 -U postgres -d puddlejumper -c "SELECT state, pickup_lat, nailed_pickup_lat FROM app_private.sm_read('UjT1hE9eBXh2q95aSZYOkzDJ8lo1');"
  -- Expected: state=UNCOMMITTED, both coords NULL

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  SUCCESS CRITERIA — PER SOLO TRIP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Discord: 🟢 UNCOMMITTED → 🟡 ENROUTE    (offer_accepted)
  Discord: 🟡 ENROUTE     → 🔵 IN_TRIP    (gps_convergence)
  Discord: 🔵 IN_TRIP     → 🟢 UNCOMMITTED (dropoff_confirmed)

  Exactly 3 Discord notifications per trip
  Exactly 3 state log entries per trip
  Zero watchdog_auto_reset entries
  Zero manual_reset entries

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  POST-DRIVE CHECKLIST
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  # 1. Pull Android logs
  adb pull /sdcard/Download/puddle_logcat.txt ~/puddle_logcat.txt

  # 2. Replay drive
  python3 ~/puddlejumper-prod/replay_drive_stateful.py \
    $(date +%Y-%m-%d)

  # 3. Check final state
  psql -h 10.128.0.2 -U postgres -d puddlejumper \
    -c "SELECT state, pickup_lat, nailed_pickup_lat
        FROM app_private.sm_read(
        'UjT1hE9eBXh2q95aSZYOkzDJ8lo1');"

  # 4. State log summary
  psql -h 10.128.0.2 -U postgres -d puddlejumper << 'SQL'
  SELECT trigger_event, from_state, to_state, COUNT(*) as n
  FROM app_private.driver_trip_state_log
  WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
    AND logged_at > NOW() AT TIME ZONE 'America/Chicago'
                   - INTERVAL '12 hours'
  GROUP BY trigger_event, from_state, to_state
  ORDER BY n DESC;
  SQL

  # 5. Narrative analysis
  python3 ~/puddlejumper-prod/drive_review.py $(date +%Y-%m-%d)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  KEY CONSTANTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Driver ID:        UjT1hE9eBXh2q95aSZYOkzDJ8lo1
  DB:               psql -h 10.128.0.2 -U postgres -d puddlejumper
  Money Market ID:  6a35d28b-8e6c-4d60-94aa-2661e2650863
  VM:               andrew@puddle-jumper via IAP tunnel
  Deploy:           cd ~/puddlejumper-prod && bash deploy.sh
  Test:             python3 ~/puddlejumper-prod/test_state_machine.py  # 47/47
  bash ~/puddlejumper-prod/test_integration.sh        # 43/43

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  NEXT SESSION PRIORITIES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  After validation drive:
  1. sm_log_decision() + sm_log_market_signal() — atomicity
  2. sm_log_nail_confirmation() — cleanup
  3. Delete _DEPRECATED_trip_state.py — permanent
  4. L0 Mission Controller — repositioning between rides

  Pending TODOs:
  - Log gpsAgeSec from Android into trace_data via
    p.get("gpsAgeSec") in decisions pipeline
  - Transition get_market_rate() to IDW interpolation
    when 500+ nail confirmations available
