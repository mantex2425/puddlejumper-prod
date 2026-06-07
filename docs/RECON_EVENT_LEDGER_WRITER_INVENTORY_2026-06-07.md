# Recon (L-6) — existing log-writer inventory for the event-ledger

**Date:** 2026-06-07 · **For:** `docs/BRAINSTORM_EVENT_LEDGER_2026-06-07.md` §9 ("recon owed
before design"). **Purpose:** inventory exactly what the three existing log writers capture, so
the ledger schema is a *provable superset* and the shadow-period assertion (§7) has a per-field
checklist. Read-only; no code changed.

## Headline findings (two reshape the consolidation plan)

1. **`driver_trip_state_log` is a DEAD writer — there is nothing live to consolidate.** No DB
   trigger writes it; no app INSERT exists (the only code reference is a READ in
   `drive_review.py:102`); and its state-transition rows (`ENROUTE`/`UNCOMMITTED`/`IN_TRIP`/
   `REFINE_DROPOFF`/`STACKED`) **stop at 2026-05-04** — the old narrative state machine the
   Sprint-A rewrite demolished. The only writes since are **manual markers** (`DRIVE_START_MARKER`
   etc., inserted by psql, debug instrumentation like `contest_labels`). ⇒ The ledger does **not
   consolidate an active writer here; it ADDS state-transition events** the current code emits
   nowhere. (Brainstorm §2.2 / §7 "the `driver_trip_state_log` inserts" is stale — there are none.)

2. **`heartbeat_log` is continuous telemetry — it STAYS, not consolidated.** Per the §3 boundary,
   the ledger references raw position from `heartbeat_log`; it does not absorb the per-heartbeat
   stream. Live INSERT (`driver_heartbeat.py:2070`): `driver_id, lat, lng, speed_mph,
   gps_accuracy_m, current_offer_id, cumulative_miles` (+ `logged_at` default). **5 vestigial
   columns** carried from the demolished state machine, always NULL now: `stopped_seconds, armed,
   target_type, dist_to_target_m, state`. (Also a 2nd writer: `scripts/replay_pudo.py:299` — replay
   harness, out of band.)

3. **`pudo_decision_context` (`_log_decision_context`, `driver_heartbeat.py:1526`) is the ONE
   substantive live writer** — per-heartbeat matcher/decision log. ~45 schema columns, but **~1/3
   are vestigial** (deprecated gates, dead planner, write-frozen, or a separate manual writer). The
   **live set** is the ledger's real superset target; the vestigial columns must NOT be carried.

## `pudo_decision_context` — live columns (the superset target + shadow checklist)

Populated by `_log_decision_context` on every heartbeat:

| group | columns (source) |
|---|---|
| context | `driver_id`, `created_at` (default), `current_offer_id_at_eval` (PRE-dispatch), `current_offer_id` (POST-dispatch fold), `lat`/`lng`, `speed_mph`, `heading`, `gps_accuracy_m`, `gps_age_s` |
| WAI/match (top) | `wai_pudo_type`, `wai_offer_id`, `wai_confidence`, `wai_target_address`, `wai_reason`, `wai_on_target_road`, `wai_current_road`, `wai_current_road_class`, `wai_off_wire_duration_s`, `wai_cluster_revisit` |
| cluster | `cluster_lat`, `cluster_lng`, `cluster_size`, `cluster_duration_s`, `cluster_started_at` |
| POI | `poi_lookup_source`, `poi_match_score`, `poi_top_names` (Head-5/Head-1 resolution) |
| dispatch | `planner_action` (executed-action string, `+`-joined), `dispatch_executed`, `dispatch_error` |
| arrest | `arrest_started_at`, `arrest_duration_s` (from the driver_trip_state UPDATE RETURNING) |
| matcher | `matched_offer_id`, `match_signal`, `matcher_candidates` (text[]), `unmatched_reason` |
| cadence | `cadence_target_hz` |
| **JSONB blobs** | `odometer_gate_result` (repurposed → `{schema_version, actual_odometer, odometer_status}` — the only point-in-time at-heartbeat odometer), `tad_decision_context` (lost_mode, last_known_anchor, queue_metadata, lock-suppressions), `wai_per_offer_scores` (per-offer signal breakdown — the forensic gold) |

**Vestigial — in the schema, NOT live-written (do NOT carry into the ledger):**
`motion_gate_result` (NULL, deprecated §XIV.A), `gate_held_offer_ids`/`gate_held_legs` (NULL,
deprecated), `planner_target_state`/`planner_corrected_lat`/`planner_corrected_lng`/`planner_reason`
(dead — old planner; only `planner_action` is live), `poi_source` (dead duplicate of
`poi_lookup_source`), `peak_confidence`/`decay_samples` (in schema, never written by this writer —
observed NULL in the 06-04 forensic), `phase_reached` (write-frozen per §XVI.C),
`ground_truth_label`/`ground_truth_notes` (NOT written here — separate manual labeling writer; the
debug ground-truth layer, sibling to `contest_labels`).

## Mapping to ledger event types

- **`pudo_decision_context` per-heartbeat row** → decomposes into ledger events at change/decision:
  - matcher state → `matcher_eval` events at inflection (`wai_*`, `matched_offer_id`, `match_signal`,
    `unmatched_reason`, `matcher_candidates`, `wai_per_offer_scores` payload) + the `matcher_snapshot`
    (§6.2) is exactly `{wai_offer_id (top), wai_confidence→tier}`.
  - cluster/arrest/cadence → sensor events (`cluster_*`, `arrest_*`, `cadence_target_hz`).
  - dispatch → PUDO/bind events (`planner_action`, `dispatch_executed`, `dispatch_error`).
  - `odometer_gate_result.actual_odometer` → carried on the event as `cumulative_miles` (already a §6 field).
  - context (`lat/lng/heading/gps_*`, `current_offer_id[_at_eval]`) → standard §6 event fields.
- **`heartbeat_log`** → NOT consolidated; ledger references it for raw continuous position (§3).
- **`driver_trip_state_log`** → no live writer to fold; the ledger emits `state_transition` events
  fresh (and absorbs the manual-marker concept as an event type — markers stop being special).

## Shadow-assertion checklist (§7) — the metrics the change-only stream must reconstruct

The assertion is: replaying the ledger's deltas+keyframes reconstructs, for any window, **every
LIVE column above** — NOT the vestigial set. Concretely, the change-only stream must let you recover:
`current_offer_id` (pre+post), the top-match (`wai_offer_id/type/confidence/reason/target_address`),
the per-offer signal breakdown (`wai_per_offer_scores`), the cluster geometry (`cluster_*`), arrest
(`arrest_*`), cadence, dispatch outcome (`planner_action`/`dispatch_executed`/`dispatch_error`),
the matcher verdict (`matched_offer_id`/`match_signal`/`unmatched_reason`/`matcher_candidates`), the
point-in-time odometer, and the TAD blob — at each inflection. The vestigial columns are explicitly
out of scope (carrying them would re-import dead weight the cutover is meant to shed).

## Refinements to the brainstorm (fold into design)

- **Consolidation scope shrinks to one table.** Only `pudo_decision_context` is a live writer to
  fold; `driver_trip_state_log` is dead (add transition events anew); `heartbeat_log` stays.
- **The superset is the LIVE set, not the schema.** ~1/3 of `pudo_decision_context`'s columns are
  vestigial; the ledger schema should be a superset of the *live-written* fields only.
- **The two JSONB blobs already exist** (`tad_decision_context`, `wai_per_offer_scores`) — the
  ledger's `payload` is largely a re-home of these, not new structure.
- **`ground_truth_label`/`ground_truth_notes` + `contest_labels`** are the debug ground-truth layer
  (manual). The ledger folds the *tap* in as an event type (§5); the labeling columns are a separate
  manual writer to leave alone.

## Still open (carried)

- Exact `matcher_snapshot` confidence-tier boundaries (must include the 0.40 floor — §6.2).
- Whether `peak_confidence`/`decay_samples`/`phase_reached` are written by ANY other path (this
  recon found none in the heartbeat writer; confirm no second writer before declaring fully dead).
- The §XVI.G `suppressed_contexts` (lock-suppression) payload shape — it rides in
  `tad_decision_context` today; confirm it becomes a first-class `lock_suppressed` event.
