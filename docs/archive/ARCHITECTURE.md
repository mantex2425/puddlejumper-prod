════════════════════════════════════════════════════════════════
  PUDDLEJUMPER — CANONICAL ARCHITECTURE
  "Postgres owns the Truth. Python owns the Strategy."
════════════════════════════════════════════════════════════════

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  THE STATE MACHINE IS A HIERARCHICAL 4-BOX CONTROLLER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Each driver state is a controller level. Each level has two
independent Monitor feeds that trigger the same
Diagnose → Plan → Execute loop.

MONITOR FEED 1: Offer card (Android accessibility service)
MONITOR FEED 2: GPS heartbeat (every 5 seconds)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  STATE LEVELS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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
                       → HOLD (driver picked up passenger,
                         normal driving — NEVER abort)

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
  IMPLICIT CANCELLATION — GPS IS ALWAYS THE TRUTH
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Uber never sends an explicit cancellation signal.
The system detects cancellations through two mechanisms:

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

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ENFORCEMENT LAYERS (three gates, every transition)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  1. Python wrapper   — rejects unknown triggers early
  2. sm_transition()  — validates against valid_state_transitions
  3. DB trigger       — enforce_state_transition_trigger, final gate

Nothing reaches the database without passing all three.

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

This guard is what allows ENROUTE → IN_TRIP to work.
Without it, picking up a passenger triggers ABORT.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  WHAT STAYS IN PYTHON — NEVER BECOMES A STORED PROC
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Decision engine threshold math    — changes frequently
  Triangulation / arc-banding       — requires Google Maps API
  YOLO/OCR pipeline                 — Android-side
  Discord notifications             — Postgres has no HTTP
  check_convergence()               — pure Python math, no DB writes
  Firebase auth                     — external service

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ADDRESSING SYSTEM — every piece of logic has an address
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  [State Level] → [Monitor|Diagnose|Plan|Execute]

  Before adding any code, ask:
    1. Which state level does this belong to?
    2. Which Monitor feed triggers it?
    3. Is it Diagnose (read), Plan (logic), or Execute (write)?
    4. If Execute — does it go through sm_transition()?
       If not — it does not belong here.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  CANONICAL RULES — NEVER VIOLATE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  STATE:    DriverStateMachine.transition() is the ONLY write path
  READ:     DriverStateMachine.read() — pure, no side effects
  ABORT:    Never fire if nailed_pickup_lat IS NOT NULL
  COORDS:   app_private.distance_miles(lat1, lng1, lat2, lng2)
  COORDS:   app_private.safe_h3(lat, lng)
  COORDS:   args always (lat, lng) — never reversed
  TIME:     NOW() AT TIME ZONE 'America/Chicago' — never bare NOW()
  TESTS:    47/47 must pass before every deploy
  DEPLOY:   bash deploy.sh from ~/puddlejumper-prod/

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  KEY CONSTANTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Driver ID:  UjT1hE9eBXh2q95aSZYOkzDJ8lo1
  DB:         psql -h 10.128.0.2 -U postgres -d puddlejumper
  VM:         andrew@puddle-jumper via IAP tunnel
  Deploy:     cd ~/puddlejumper-prod && bash deploy.sh
  Test:       python3 ~/puddlejumper-prod/test_state_machine.py
