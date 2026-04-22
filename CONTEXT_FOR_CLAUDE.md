# CONTEXT FOR CLAUDE — Working with Andrew on PuddleJumper

Intended audience: a future Claude instance (likely me after context reset)
picking up mid-project. Read this first before proposing anything.

## How Andrew works

- **ONE STEP AT A TIME.** Never chain multiple actions in one response.
  Wait for explicit confirmation before proceeding.
- **CLI and psql ONLY.** Andrew copies commands into his terminal session
  on the puddle-jumper VM. Never propose Python REPL sessions or "just
  run this locally." Use `python3 <<'PYEOF' ... PYEOF` heredocs when
  Python is needed — they run in one shot.
- **LONG OUTPUT GOES TO FILES.** For any command producing >20 lines,
  redirect to `/tmp/*.txt` and `cat` at the end. Terminal wrapping eats
  output. File-backed output is copy-pastable.
- **TESTABLE VERIFICATION per step.** Every action ends with a specific
  way to confirm it worked: grep for a string, run a test, check a
  column, inspect a file. Never "it should work" — prove it.
- **Andrew is a veteran programmer (since 1995).** Explain MODERN tools
  and terminology; do NOT over-explain core logic.
- **Push back when direction is wrong.** Don't just comply. Challenge
  "while we're here" impulses. Honor deliberate decisions.
- **No verbose preambles.** Answer directly. Explain choices only when
  asked or when the choice has tradeoffs worth surfacing.
- **Python heredocs for code edits** — NEVER sed for code. sed eats
  special characters and makes surgical edits unreviewable.

## PuddleJumper Non-Negotiable Rules

### 4-Box Controller Architecture

Every file belongs to exactly ONE box:

- **MONITOR**: receives sensor inputs. No logic, no writes.
  → driver_heartbeat.py (input receipt layer), Android accessibility svc
- **DIAGNOSE**: interprets sensors. Pure READS only. No writes.
  → nail_it_core.check_convergence()
  → decisions/state_enricher.py (read state)
- **PLAN**: business logic and strategy. NO DB writes.
  → decisions/router.py
  → decisions/engine.py
  → decisions/triangulation_enricher.py
- **EXECUTE**: the ONLY write path.
  → DriverStateMachine.transition() → sm_transition() in Postgres

**Violation pattern:** if you see a DB write outside EXECUTE, or a
transition() call outside PLAN→EXECUTE handoff, STOP and redesign.

Before adding any code, ask:
1. Which state level does this belong to?
2. Which Monitor feed triggers it?
3. Is it Diagnose (read), Plan (logic), or Execute (write)?
4. If Execute — does it go through sm_transition()? If not, it
   doesn't belong here.

### Enforcement layers (three gates, every transition)
1. Python wrapper: rejects unknown triggers early
2. sm_transition(): validates against valid_state_transitions
3. DB trigger enforce_state_transition_trigger: final gate

`SET LOCAL app.state_trigger` is handled by sm_transition() internally
— never call it directly from Python.

### Coordinate operations
- ALWAYS: `app_private.coords_to_h3(lat, lng)`, `h3_to_lat(h3)`,
  `h3_to_lng(h3)`, `coords_to_point(lat, lng)`,
  `coords_to_geography(lat, lng)`, `distance_miles(lat1,lng1,lat2,lng2)`
- args ALWAYS `(lat, lng)` — never reversed
- NEVER: `ST_MakePoint`, `h3_latlng_to_cell`, `h3_cell_to_latlng`,
  raw H3 functions, UTC offsets directly

### Time
- Storage: bare `NOW()` on timestamptz columns
- Display / analytics ONLY: `NOW() AT TIME ZONE 'America/Chicago'`

### Infrastructure boundaries
- The 4-box governs ride-lifecycle state invariants.
- Side-car caches (geocode_cache, telemetry, logs) are infrastructure
  OUTSIDE the 4-box model. Document this explicitly in any such file.
- Example: `geo_utils.py` has Google geocode + DB cache writes. It is
  infrastructure. Its cache writes do not need to flow through
  sm_transition(). Its docstring says so.

### Deploy
- Only after full test suite passes (55/55 state_machine, 61/61 integration)
- `bash deploy.sh` from ~/puddlejumper-prod/
- Branch: patch-00566a-unified-refinement
- Current prod revision: puddlejumper-api-00570-g9m (100% traffic)

## Infrastructure

- VM access: `gcloud compute ssh andrew@puddle-jumper --tunnel-through-iap`
- DB: `psql -h 10.128.0.2 -U postgres -d puddlejumper`
- App DB user: `atjb` (needs explicit GRANTs for new tables/procs)
- Driver ID: `UjT1hE9eBXh2q95aSZYOkzDJ8lo1`
- Money Market ID: `6a35d28b-8e6c-4d60-94aa-2661e2650863`

## Where we are (2026-04-22 afternoon)

### Arc-band nuke in progress
We're removing a pile of "fungal growth" — arc-band triangulation,
Scorer A/B (polyline contest mode), REFINE_DROPOFF state, Watchdog A/B.
Replaced by bead-on-wire (a Blind Man's walking-stick heuristic:
terminal pivot off named road + odometer floor + low-speed cluster).

### Completed
- Phase 1 (495cded): geo_utils.py extracted with 4 geocode helpers.
  Honest docstring declares it infrastructure outside 4-box.
- Phase 2a (3e9a4f9): 7 dead backtest/seed scripts deleted.
  decision_engine_v1 (both overloads) dropped from Postgres.
- Phase 2b partial (de769cc): arc-band imports removed from
  pickup_confirm.py, decisions/engine.py, decisions/router.py,
  driver_heartbeat.py. Comment update in refinement_gates.py. All
  with FIXME markers for pre-existing 4-box violations deferred to
  post-bead refactor.

### Remaining Phase 2b
1. `decisions/triangulation_enricher.py` — LOAD BEARING rewrite:
   - KEEP: geocoded-miles validation, confidence tier assignment,
     shadow signal writes to pickup_market_signals, and the
     DriverStateMachine.transition() offer_accepted handoff.
   - REMOVE: triangulate_pickup, triangulate_dropoff,
     _cache_trip_polyline_on_accept, _cache_stacked_polyline_async,
     refine_dropoff_background.
   - RESULT: triangulated_pickup_lat/lng become just ep['p_lat']/
     ep['p_lng'] (the Google-geocoded coords). Same output schema
     (triangulatedPickupLat in result dict), just populated simpler.
2. `nail_it_core.py` line 173: remove `from routes_api import
   fetch_feasible_change as _fetch_feasible_change` and the
   Feasible Change / Scorer A/B block it feeds.
3. `git rm decisions/nail_manager.py` (callers clean as of de769cc).
4. `git rm arc_band.py triangulation.py routes_api.py`.

### Phase 3 (REFINE_DROPOFF removal)
Files: test_state_machine.py, decisions/triangulation_enricher.py,
state_machine.py, driver_heartbeat.py, test_integration.sh,
nail_it_core.py, dropoff_confirm.py. SQL migration to remove from
valid_state_transitions. Delete tests T45/T46/T49.

### Phase 4 (Watchdog A/B removal)
Files: test_state_machine.py, monitor.py, state_machine.py,
driver_heartbeat.py, test_integration.sh, nail_it_core.py,
drive_review.py. Delete watchdog tests.

### Phase 5 (Scorer A/B removal)
Files: routes_api.py (deletion covers), triangulation_enricher.py
(rewrite covers), driver_heartbeat.py, nail_it_core.py. Contest
mode blocks.

## THE REAL BUG — WHY WE'RE DOING ALL THIS

**Ride 6535, 2026-04-22 05:45:57 — University Blvd, Sugar Land.**

Morning drive produced 8 dire dropoffs (errors 7243m / 2874m /
25324m). Ride 6535 replay through bead-on-wire shows ALL 3 gates
passed:
- Gate 1 odometer: 8.93mi vs 7.83mi floor ✓
- Gate 2 pivot context: last_named_road="University Boulevard" matches
  "University Blvd" ✓ (pivot 05:45:26, 31s before dropoff press)
- Gate 3 cluster: n=3, tight, median (29.5511, -95.5843) ✓

**Bead-on-wire SHOULD have fired. It did not.**

Classification: `single_road` — Blind Man path applies.

State transitions during the ride:
- 05:09:58 UNCOMMITTED → ENROUTE (offer_accepted, 7159)
- 05:25:20 ENROUTE → IN_TRIP (gps_convergence)
- 05:39:38 IN_TRIP → STACKED (offer_accepted, 7160)
- 05:46:50 STACKED → ENROUTE (dropoff_confirmed) ← fired at WRONG coords
- 05:59:30 ENROUTE → IN_TRIP (gps_convergence, 7160's pickup)

The 05:46:50 dropoff_confirmed fired at coords 7243m off target.
Bead-on-wire computed the correct target but its override in
check_convergence is passive — the existing distance-threshold logic
AFTER our override still has the final say. That's the bug.

### The fix (after the nuke is done)

In `nail_it_core.check_convergence`, when bead-on-wire's
`compute_target()` returns with all 3 gates passed, return the
NAIL verdict DIRECTLY — bypass the distance check entirely.

No writes from DIAGNOSE. Just a new verdict path. PLAN layer
(driver_heartbeat.py) already dispatches on INITIAL_NAIL /
DROPOFF_NAIL verdicts and calls sm_transition with the matching
trigger.

This is why we're ripping out REFINE_DROPOFF / Watchdog A/B /
Scorer A/B first: three parallel fire-decision systems were
overriding bead-on-wire's signal. Clean them out, then bead-on-
wire's verdict has nothing competing with it.

## Known FIXMEs (deferred)
- pickup_confirm.py ~line 171: pre-existing 4-box violation, direct
  UPDATEs from PLAN layer. Deferred until bead-on-wire validated.
- driver_heartbeat.py ~line 298: same class, on pickup_market_signals
  and community_offers UPDATEs.
- STACKED redesign (STACKED_REDESIGN.md exists, deferred until after
  first clean solo Auto Nail It).
- Play Store submission blocker (accessibility service disclosure video).
- drive_review.py trigger label list still has REFINE_DROPOFF — update
  when Phase 3 runs.
- replay_drive_stateful.py will break when enrich_with_triangulation is
  rewritten — fix after Phase 2b.
- Mortgage P&L (Jan 2025–Mar 2026): external deadline, separate context.

## Testing invariants
- 55/55 state_machine tests expected after each commit
- 61/61 integration tests (scope varies as REFINE_DROPOFF tests are deleted in Phase 3)
- pickup_confirm.py:84 KeyError 'dist_m' on endpoint test is
  pre-existing, unrelated to bead work

## Files and their boxes (quick reference)
- MONITOR: driver_heartbeat.py (input receipt part only)
- DIAGNOSE: nail_it_core.py, decisions/state_enricher.py, bead_on_wire.py
- PLAN: decisions/router.py, decisions/engine.py,
  decisions/triangulation_enricher.py, pickup_confirm.py,
  dropoff_confirm.py (endpoint handlers)
- EXECUTE: state_machine.py (DriverStateMachine), Postgres sm_transition()
- INFRASTRUCTURE (outside 4-box): geo_utils.py, db.py, utils.py
