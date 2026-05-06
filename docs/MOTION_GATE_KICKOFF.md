# Motion Gate Sprint — Kickoff Handoff (v2)

**Date authored:** 2026-05-06 (late session, after full-day Phase 2c.2 architectural recon)
**Sprint scope:** Implement Sprint A §7 Motion Gate, EXTENDED to include odometer gate (porting BEAD's `_BLIND_MAN_TORT_MIN` to WAI). Single-purpose sprint, dual-protection deliverable.
**Estimated effort:** Approximately one day, including ratification, implementation, and validation against historical drives.
**Predecessor commit:** `f4ef3ed` on branch `demolition-2026-05-04` (Patch 2b).
**Successor sprint:** Phase 2c.2 (Head 4 / Semantic Type Alignment) — resumes AFTER both gates ship and validate.

---

## Why this sprint exists

### The architectural gap

`docs/SIMPLIFIED_ARCHITECTURE.md` is "Product Law for Sprint A" per `docs/INDEX.md`. Section §7 specifies a Motion Gate:

> "If cluster speed >= 2mph or cluster duration < threshold, do not fire a transition. Log the WAI evaluation, return without state change. Prevents drive-by false positives."

**Both Motion Gate AND odometer gate are unimplemented in current production code.** Verified on 2026-05-06 by recon:

```
$ grep -nE "motion_gate|MOTION_GATE|drive.by" driver_heartbeat.py
# (empty output)
```

The matcher invocation in `driver_heartbeat.py` (around line 588-589) runs unconditionally:

```python
wai = WhereAmI(cur)
matches, diagnostics = wai.evaluate_with_diagnostics(driver_id, snap.offers)
```

There is no speed-and-duration gate, AND no odometer gate, between cluster detection and matcher evaluation.

### The §7 documentation gap

§7 specifies speed-and-duration but does NOT specify an odometer gate. This was a documentation gap, not an architectural choice. The odometer gate lived in `bead_on_wire.compute_target` (BEAD system) as a peer protection alongside on-wire and motion checks. When BEAD was deprecated in the demolition refactor, both gates needed to migrate to WAI, but only Motion Gate (speed-and-duration) was specified in §7.

The odometer gate is real and architecturally necessary. From `bead_on_wire.py` lines 644 and 712-720:

```python
_BLIND_MAN_TORT_MIN = 0.9

# Inside compute_target, for poi bucket:
odometer = odometer_miles_since(driver_id, anchor_time, cur)
min_miles = expected_miles * _BLIND_MAN_TORT_MIN if expected_miles else 0.0
if expected_miles and odometer < min_miles:
    logging.info(
        f"[BEAD] poi {address_text!r}: odometer {odometer:.2f}mi "
        f"< floor {min_miles:.2f}mi -> HOLD"
    )
    return None
```

Same pattern at lines 771-779 for `single_road` bucket. Both are dead in production because `compute_target` itself is never called in current code paths -- but the threshold constant `_BLIND_MAN_TORT_MIN = 0.9` is the canonical value to port to WAI.

### Why these gates block Phase 2c.2

Phase 2c.2 is replacing the lexical Heads 1-3 in `_signal_poi_match` with a semantic Head 4 that consults Google Place Types. Empirical audit on 2026-05-06 confirmed Head 4 finds commercial POI types (`grocery_store`, `restaurant`, `dentist`, etc.) within 150m of every commercial-area stop in Houston. Houston has no zoning, so even residential clusters return 1-2 commercial POIs at the edges of the search radius.

**Without dual gates**, two failure modes exist:

1. **Drive-by false positive (Motion Gate / speed-and-duration territory):** driver passes a strip mall at 30+mph; cluster doesn't form ideally, but if it does, Motion Gate prevents matcher fire.

2. **Mid-route long stop false positive (odometer gate territory):** driver hits a 2-minute traffic light at a major intersection halfway through trip. Speed=0, duration=120s. Speed-and-duration gate PASSES (this looks like a real PUDO). Without odometer gate, matcher fires. Head 4 finds a Walmart/Shell/CVS within 150m, scores high, and the system fires a dropoff PUDO when the driver is still 2 miles from the actual destination.

The 2026-05-04 validation data has a real example: Drive 1, Stop 1A at 14:51-14:53 -- 20 heartbeats stationary for 2 minutes at `29.4975076, -95.518549`, mid-route, ~1 mile into a 5-mile trip. Speed-and-duration gate alone would PASS this cluster. Only the odometer gate (1mi < 0.9 * 5mi = 4.5mi) holds it.

Shipping Head 4 onto a single-gate matcher amplifies a structural problem. The fix is implementing BOTH gates together, since they protect against different failure modes.

### What the existing code DOES have

`cluster_detection.py` enforces formation thresholds:
- `max_spread_m=25.0` -- clusters only form within 25m
- `min_cluster_duration_s` (configurable) -- stationarity duration

These ARE protective for cluster FORMATION, not matcher invocation given a cluster. The dual gates operate at a different layer: even when a cluster has formed, evaluate (a) is the driver in motion, (b) has the trip progressed far enough for a dropoff PUDO to be plausible.

A long traffic light at a major intersection (90s+ stationary, 5m spread) DOES form a cluster under existing thresholds. Both Motion Gate AND odometer gate are required to prevent the matcher from interpreting that cluster as a PUDO.

---

## Scope of THIS sprint

### Deliverables

1. Read `docs/SIMPLIFIED_ARCHITECTURE.md` §7 in full (the recon-extracted excerpt was partial -- read the full section)
2. Read `bead_on_wire.py` lines 636-820 to understand the odometer gate's existing implementation in BEAD
3. Design BOTH gates as a unified protective layer:
   - Motion Gate (speed-and-duration per §7)
   - Odometer gate (porting `_BLIND_MAN_TORT_MIN = 0.9` from BEAD to WAI's flow)
4. Wire both gates between cluster detection and `wai.evaluate_with_diagnostics()` invocation in `driver_heartbeat.py`
5. Add tests covering:
   - Real PUDO clusters pass both gates
   - Drive-by clusters held by Motion Gate
   - Mid-route stop clusters held by odometer gate
   - Both-gates-pass cases at trip end
6. Validate against historical drive data from `heartbeat_log` (2026-05-04 and 2026-05-05 drives -- see "Validation Data" below)
7. Single atomic commit (or sequenced commits if the dual-gate work is genuinely separable, which it probably isn't)
8. Pytest green at boundary

### Explicit non-scope

**Do NOT do any of the following:**
- Touch `where_am_i.py` matcher bodies (Phase 2c.2 territory)
- Touch `_signal_poi_match` or `_signal_*` matcher logic
- Touch `_CONFIDENCE_WEIGHTS` table
- Modify `poi_service.py` (e.g., do not bump `API_SEARCH_RADIUS_M`)
- Modify the address classifier (`bead_on_wire.classify_address`)
- Refactor the Heads 1-3 lexical matching
- Add Head 4 / Semantic Type Alignment

If the user (Andrew) drifts toward Phase 2c.2 territory mid-sprint, push back politely and refer to this doc. Phase 2c.2 has its own design ratified and resumes after this sprint validates.

### When this sprint is complete

- Motion Gate AND odometer gate are in production
- §7 of SIMPLIFIED_ARCHITECTURE.md is updated to reflect that the gate layer includes both protections (was a documentation gap; now fixed)
- Tests pass (the existing test floor was 354/354 before; should remain green)
- A historical replay confirms:
  - Real PUDO clusters (Drive 1 Stop 1C, Drive 2 Stop, Dentist Stop 2): BOTH gates pass
  - Drive-by motion (mid-drive 50mph heartbeats): cluster doesn't form, matcher doesn't run
  - Long mid-route stops (Drive 1 Stop 1A): Motion Gate would pass, odometer gate HOLDS
  - Quick taps (brief 5s stops): Motion Gate HOLDS regardless of trip progress
- Commit lands on `demolition-2026-05-04` branch (or successor branch -- confirm with Andrew)
- Phase 2c.2 sprint is unblocked

---

## Required first reads

The new Claude session should view these in order before authoring anything:

1. `docs/SIMPLIFIED_ARCHITECTURE.md` -- §7 in full, plus §3 (Map-Reduce contract), §5 (match resolution), §11 (sprint scope)
2. `docs/SPRINT_PLAN.md` -- Sprint A section to confirm the Motion Gate hasn't already shipped under a different name
3. `docs/CANONICAL_RULES.md` -- coordinate functions, UTC, 4-box controller
4. `docs/SESSION_PROTOCOL.md` -- paired-programming cycle, CLI-first, paste-safety rules
5. `driver_heartbeat.py` lines 540-620 -- current matcher invocation flow; also lines 520-530 for `cumulative_miles` payload
6. `cluster_detection.py` -- `Cluster` dataclass, `is_stable()` helper, `min_cluster_duration_s` lookup
7. `bead_on_wire.py` lines 636-820 -- odometer gate's existing implementation in BEAD (`compute_target`, `_BLIND_MAN_TORT_MIN`, `odometer_miles_since`)
8. `where_am_i.py` lines 1-100 -- `WhereAmI` class signature

**Do NOT view** `where_am_i.py` matcher bodies (`_signal_*`, `_match_*`) or `_CONFIDENCE_WEIGHTS`. Those are Phase 2c.2 territory.

---

## Architectural starting points

These are hypotheses, not ratified design. The new session should confirm or amend with Gemini before authoring.

### Hypothesis A: Combined gate function

Single function with sub-checks, probably in a new `motion_gate.py` module (since "Motion Gate" is becoming "gate layer" with two protections). Signature roughly:

```python
def gate_layer_passes(
    cluster: Cluster,
    recent_heartbeats: list[Heartbeat],
    current_offer: Optional[Offer],
    cumulative_miles_at_evaluation: float,
    pickup_miles: Optional[float],
    threshold_s: float = 15.0,
    max_speed_mph: float = 2.0,
    odometer_floor_fraction: float = 0.9,
) -> tuple[bool, str]:
    """Gate layer: Motion Gate (speed+duration) + Odometer Gate (trip progress).

    Returns (passes, reason).

    Motion Gate (per SIMPLIFIED_ARCHITECTURE.md §7):
      - cluster.duration_s >= threshold_s  (stationary long enough)
      - max recent speed_mph < max_speed_mph  (not in motion)

    Odometer Gate (ported from BEAD `_BLIND_MAN_TORT_MIN`):
      - For dropoff candidates: cumulative_miles_at_eval >= pickup_miles * 0.9
      - For pickup candidates: gate doesn't apply (pickup happens at trip start)

    Returns:
      (True,  "passed")
      (False, "speed=8.3mph")
      (False, "duration=4s<15s")
      (False, "odometer=1.2mi<3.5mi")
    """
```

`reason` returns a short string for forensic logging.

The pickup-vs-dropoff distinction matters: odometer gate only applies when evaluating dropoff PUDOs. For a pickup PUDO, the driver hasn't started the trip yet, so trip progress is undefined. Confirm with Gemini whether the gate applies asymmetrically or whether the function takes a `pudo_type` parameter.

### Hypothesis B: Wired between cluster formation and matcher invocation

In `driver_heartbeat.py`, current flow is:

```python
# Existing
cluster = detect_cluster(...)  # may return None
wai = WhereAmI(cur)
matches, diagnostics = wai.evaluate_with_diagnostics(driver_id, snap.offers)
```

Proposed:

```python
# After dual gates
cluster = detect_cluster(...)
if cluster is None:
    # No cluster, no evaluation
    matches, diagnostics = [], empty_diagnostics()
else:
    gate_pass, gate_reason = gate_layer_passes(
        cluster=cluster,
        recent_heartbeats=recent_hbs,
        current_offer=current_offer,
        cumulative_miles_at_evaluation=body.get('cumulative_miles', 0.0),
        pickup_miles=current_offer.pickup_miles if current_offer else None,
    )
    if not gate_pass:
        log.info(f"[gate_layer] held: {gate_reason}")
        matches, diagnostics = [], empty_diagnostics_with_reason(gate_reason)
    else:
        wai = WhereAmI(cur)
        matches, diagnostics = wai.evaluate_with_diagnostics(driver_id, snap.offers)
```

Verify the actual existing flow before assuming this structure. The recon showed lines 588-589 but the surrounding flow may differ.

### Hypothesis C: Threshold values

**Motion Gate (per §7):**
- Speed: `max_speed_mph = 2.0` (per §7)
- Duration: check `routing.known_stops_config.min_cluster_duration_s` for canonical value (mentioned at `cluster_detection.py:87`). Likely 15-20 seconds.

**Odometer gate (per BEAD):**
- Floor fraction: `_BLIND_MAN_TORT_MIN = 0.9` (canonical value, port verbatim)
- `pickup_miles` source: `offer_history.pickup_miles` column (Uber-reported trip distance)

**Speed signal source:** §7 says "cluster speed >= 2mph". Almost certainly means recent heartbeat max speed_mph (drift speed within a 25m cluster is by definition tiny). Query last 5-10 heartbeats for the driver, take max speed_mph.

**Cumulative miles source:** `body.get('cumulative_miles')` from heartbeat payload (`driver_heartbeat.py:524`). This is the driver's odometer reading at the time of the heartbeat being evaluated.

### Hypothesis D: pudo_decision_context schema extension

If the gate layer holds, WAI evaluation doesn't run. We still want forensic visibility:

```sql
ALTER TABLE app_private.pudo_decision_context
  ADD COLUMN gate_layer_result TEXT;
-- values: 'passed', 'speed_too_high', 'duration_too_short',
--         'odometer_floor_pickup_miles', 'no_cluster'
```

Verify schema change is appropriate with Andrew before proposing migration.

---

## Validation Data

Real driver GPS data is available for validation in `heartbeat_log`. Driver ID: `UjT1hE9eBXh2q95aSZYOkzDJ8lo1`.

### 2026-05-05 dentist drive (single-stop validation)
- Window: 14:15-14:27 UTC (09:15-09:25 CDT)
- Trip distance: ~3.8mi round trip (out and back)
- Stop 1 (home pickup): `29.5063, -95.5023` -- 09:15-09:18 stationary
- Stop 2 (dentist DROPOFF): `29.510316, -95.527151` -- 09:20:11-09:21:39 (88s, 5 consecutive 0mph heartbeats)
- Stop 3 (home return): `29.5063, -95.5023` -- 09:25:40+

**Expected gate behavior:**
- Stop 2 cluster (88s stationary, end-of-outbound-leg): BOTH gates pass -- real PUDO at outbound destination
- Mid-drive heartbeats at 50mph: no cluster forms (existing `cluster_detection.py` protection)

### 2026-05-04 two-drive validation
Window: 19:43-21:00 UTC

**Drive 1 (offer 7711, Comfort Reserve, dropoff "Sienna Pkwy", trip = 5.5mi per offer card):**
- Pickup at Brushy Lake & Spice Ridge ~14:43
- **Stop 1A 14:51-14:53** at `29.4975076, -95.518549` -- 20 heartbeats stationary, 2 minutes
  - **CRITICAL TEST CASE.** Cumulative miles at this stop: ~1.0mi out of 5.5mi total. 1.0 < 0.9 * 5.5 = 4.95mi. **Odometer gate must HOLD this cluster.**
  - Motion Gate alone would PASS (stationary 120s, speed=0). Without odometer gate, matcher would fire false dropoff.
- Stop 1B 14:55-14:57 at `29.4944391, -95.5038588` -- brief return near pickup (13 heartbeats)
  - Cumulative miles ~1.5mi. Odometer gate HOLDS.
- **Stop 1C 15:03-15:05+** at `29.5102826, -95.5272512` -- 30+ heartbeats, 3 minutes (Excel Dental plaza, the actual dropoff)
  - Cumulative miles ~5.5mi. **BOTH gates pass.** Real PUDO.

**Drive 2 (offer 7714, UberX Priority, dropoff "US-90-ALT", trip = 30.8mi per offer card):**
- **Stop 2 final 15:56-16:00+** at `29.5912088, -95.6011899` -- 52 heartbeats, 5+ minutes
  - Cumulative miles ~30+mi. **BOTH gates pass.** Real PUDO at strip mall.

### Validation script idea

After implementing gate layer, write a script `scripts/validate_gate_layer.py`:

```python
# Pseudocode
test_cases = [
    {
        "label": "Drive 1 Stop 1A (mid-route 2-min light)",
        "cluster_coords": (29.4975076, -95.518549),
        "duration_s": 120,
        "max_recent_speed_mph": 0,
        "cumulative_miles": 1.0,
        "pickup_miles": 5.5,
        "expected_pass": False,  # odometer gate must hold
        "expected_reason_substring": "odometer",
    },
    {
        "label": "Drive 1 Stop 1C (real dropoff at Excel Dental)",
        "cluster_coords": (29.5102826, -95.5272512),
        "duration_s": 180,
        "max_recent_speed_mph": 0,
        "cumulative_miles": 5.5,
        "pickup_miles": 5.5,
        "expected_pass": True,
    },
    {
        "label": "Drive 2 Stop (real dropoff at US-90-ALT strip mall)",
        "cluster_coords": (29.5912088, -95.6011899),
        "duration_s": 300,
        "max_recent_speed_mph": 0,
        "cumulative_miles": 30.5,
        "pickup_miles": 30.8,
        "expected_pass": True,
    },
    {
        "label": "Synthetic: brief 5s tap (Motion Gate held)",
        "duration_s": 5,
        "max_recent_speed_mph": 0,
        "cumulative_miles": 5.0,
        "pickup_miles": 5.5,
        "expected_pass": False,
        "expected_reason_substring": "duration",
    },
    {
        "label": "Synthetic: rolling 5mph (Motion Gate held by speed)",
        "duration_s": 30,
        "max_recent_speed_mph": 5.0,
        "cumulative_miles": 5.0,
        "pickup_miles": 5.5,
        "expected_pass": False,
        "expected_reason_substring": "speed",
    },
]
for case in test_cases:
    actual_pass, actual_reason = gate_layer_passes(**case_inputs)
    assert actual_pass == case["expected_pass"], f"{case['label']} pass mismatch"
    if not case["expected_pass"]:
        assert case["expected_reason_substring"] in actual_reason, f"{case['label']} reason mismatch"
```

Real PUDO cases pass. Mid-route, drive-by, and brief-tap cases held by appropriate gate. The reason string identifies which gate caught it (forensic clarity).

---

## Production environment reference

- VM: `andrew@puddle-jumper`
- Working dir: `~/puddlejumper-prod/`
- DB host: `10.128.0.2`
- DB name: `puddlejumper`
- Deploy: `bash deploy.sh`
- Cloud Run: `us-central1`
- Repo: `github.com/mantex2425/puddlejumper-prod`
- Driver ID for validation: `UjT1hE9eBXh2q95aSZYOkzDJ8lo1`
- Branch: `demolition-2026-05-04` (or successor -- confirm with Andrew)

### Canonical rules (verbatim from CANONICAL_RULES.md)

- COORDS: `app_private.coords_to_h3(lat,lng)`, `coords_to_geography(lat,lng)`, `distance_miles(lat1,lng1,lat2,lng2)`. Args always `(lat,lng)`. NEVER write `ST_MakePoint` directly.
- TIME: UTC mandatory. `NOW()` (bare) in Postgres. Texas Time at UI edge ONLY.
- 4-Box Controller: DIAGNOSE / PLAN / EXECUTE / OBSERVE. Gate layer sits upstream of DIAGNOSE.
- All DB writes via `sm_transition()` stored procedure. Gate layer doesn't write; it gates write triggers.

### Session protocol highlights

- Paired-programming: Claude proposes -> Gemini reviews -> consensus -> execute
- CLI-first execution instructions to Andrew
- SQL wrapped in psql heredocs OR `psql -c` quoted strings
- Long outputs to file + `cat`
- No raw multi-line markdown paste to bash; scp from `/mnt/user-data/outputs/` to VM
- Apply scripts go in `~/puddlejumper-prod/tmp/`
- Step-by-step with explicit confirmation checkpoints (Andrew's preference)

---

## What the new Claude session should NOT assume from prior context

The prior session (2026-05-06) accumulated significant Phase 2c.2 architectural work:
- 36-address audit at 50m and 150m radius
- 5-cluster real-GPS audit across two drives
- Head 4 / CLASS_TO_TYPE_MAP design ratified by Gemini
- Multiple weight-table revisions for `single_road` and `apartment_complex`
- Discussion of cache TTL, off-wire-pivot reweighting, classifier behavior

**None of that is relevant to the gate layer sprint.** The new session should treat this as a focused Sprint A completion. If Andrew references "Head 4" or "Phase 2c.2" mid-sprint, redirect to: "Phase 2c.2 resumes after the gate layer ships and validates. Let's stay focused on this sprint."

---

## Recommended first message to the new Claude session

Here's a copy-pasteable opener for Andrew to start the new chat:

> Hi Claude. I'm starting a focused sprint to implement the gate layer protecting the WAI matcher: Motion Gate (per `docs/SIMPLIFIED_ARCHITECTURE.md` §7) PLUS odometer gate (porting BEAD's `_BLIND_MAN_TORT_MIN = 0.9`).
>
> A previous session (2026-05-06) completed extensive recon for a Phase 2c.2 (Head 4) architecture and discovered that BOTH gates were either specified-but-unimplemented (Motion Gate) or implemented-in-dead-code (odometer gate, in BEAD's deprecated `compute_target`). Without both gates, Phase 2c.2 ships onto a vulnerable matcher that would false-fire at traffic lights mid-route AND at lights near the destination. So Phase 2c.2 is paused until both gates ship together.
>
> Please read `~/puddlejumper-prod/docs/MOTION_GATE_KICKOFF.md` (this handoff doc) first. It contains:
> - Why this sprint exists (architectural gap, both gates)
> - Explicit scope and non-scope
> - Required first reads
> - Architectural starting points (hypotheses, not ratified design)
> - Validation data from real production drives, including the critical Drive 1 Stop 1A test case
> - Environment reference
>
> After reading the kickoff, please read:
> 1. `docs/SIMPLIFIED_ARCHITECTURE.md` §7 in full
> 2. `bead_on_wire.py` lines 636-820 for the odometer gate's existing (dead) implementation
>
> Confirm your understanding of:
> 1. Both gates' positions in the architecture (upstream of `wai.evaluate_with_diagnostics()`)
> 2. The threshold values (Motion Gate: speed>=2mph, duration<15-20s; Odometer: cumulative<0.9*pickup_miles for dropoff candidates)
> 3. Where in `driver_heartbeat.py` the gate layer should wire in
> 4. The pickup-vs-dropoff asymmetry of odometer gate
>
> Then propose a design for paired ratification with Gemini. Do NOT touch `where_am_i.py` matcher bodies or `_CONFIDENCE_WEIGHTS` -- those are Phase 2c.2 territory and explicitly out of scope.
>
> Verification at completion: gate layer ships, tests green, validation script confirms real PUDO clusters pass and mid-route stops held by odometer, brief taps held by Motion Gate. Phase 2c.2 then resumes in a follow-up session.

---

## Handback to Phase 2c.2

When the gate layer is shipped and validated, the user should start ANOTHER new chat. This session should author `docs/PHASE_2C_2_RESUMPTION.md` at the end of its sprint covering:

- Confirmation gate layer is live (both Motion Gate and odometer gate)
- Verification log from validation script
- The full Phase 2c.2 design that was ratified on 2026-05-06:
  - Head 4 / Semantic Type Alignment (replaces lexical Heads 1-3)
  - 150m search radius (`API_SEARCH_RADIUS_M` 50->150)
  - `CLASS_TO_TYPE_MAP` constant in `where_am_i.py`
  - `_signal_poi_type_match` function with linear distance decay
  - Single_road weight: `poi_match` 0.30->0.40 (offset by reductions in breadcrumb_match and on_target_road)
  - Delete `apartment_complex` weight row entirely (Path 2)
  - Delete `_match_apartment_complex` function and `_CLASS_DISPATCH` entry (Option C)
  - NO off_wire_pivot resurrection on number_on_street (stays 0.00; gate layer protects)
  - NO Trip-Progress dynamic threshold (Gemini's late memo addition, rejected)
  - NO Confusion Trigger conditional API call (rejected as premature optimization)
  - 30-day cache TTL (ratified)
- The audit data summaries (v1, v2, real-cluster, two-drive)
- The apply script outlines (production code script + test script, single atomic commit)

---

## Final note

The user (Andrew) has been driving this architectural pivot for ~12 hours today. He values:
- Step-by-step instructions with explicit confirmation checkpoints
- Push-back when something doesn't make sense
- Architectural honesty over forward motion
- Empirical validation before code commits
- Direct, non-flattering communication

The user's working pace is his own to decide. Don't suggest breaks or pauses. If the chat is getting long, surface that explicitly as a context-window concern, not a wellness suggestion.

End of kickoff doc.