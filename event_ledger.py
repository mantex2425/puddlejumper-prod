"""event_ledger.py — §VIII Passive-lane emit helpers for app_private.event_ledger.

OBSERVABILITY ONLY (§VIII Passive lane): every write here is append-only, best-effort,
never read by a runtime decision, never alters authoritative state. Wired into
post_heartbeat inside the C4 batched savepoint (all emits + the seed-advance live or
die together; batch-level swallow, NOT per-emit).

§XIV.H note — why the reap-reason classifier is NOT a second liveness gate:
`LIVE_OFFER_PREDICATE_SQL` (driver_queue.py:192-265) remains the SINGLE home of the
"is this offer live" decision. `classify_reap` here is an *observability labeler* — it
explains WHY an offer the predicate ALREADY reaped left the set, post-hoc, reusing the
single-owner constants (GC_*) and band primitive (pudo_types.odometer_band). It never
decides liveness. Pinned to the predicate by tests/test_event_ledger_clause_mapping.py.

Status: greenfield Phase 1 — emit ALONGSIDE the untouched legacy writers
(_log_decision_context, heartbeat_log). NOT wired yet; this module is inert until
post_heartbeat calls it (separate, tested step).
"""
import datetime
import json
import logging

from driver_queue import GC_ABANDONMENT_CEILING_HOURS, GC_ODOMETER_FREEZE_MINUTES
from pudo_types import odometer_band, ODOMETER_STATUS_ABANDONED

log = logging.getLogger(__name__)

# ── reap reasons: mirror the SIX AND-ed LIVE_OFFER_PREDICATE_SQL clauses ──────────
#   (driver_queue.py:192-265 — clause numbers below match that source)
REAP_TERMINATED  = "terminated"     # clause 1: oh.actual_dropoff_at IS NULL  -> failed
REAP_CAUSALITY   = "causality"      # clause 2: oh.created_at <= ref           -> failed (future offer)
REAP_CEILING     = "ceiling"        # clause 3: oh.created_at > ref - 4h        -> failed (aged out)
REAP_STALENESS   = "staleness"      # clause 4: odometer-freeze gate            -> failed
REAP_BAND        = "band_overshot"  # clause 5: upper-edge band (per leg)       -> overshot
REAP_ABANDONED   = "abandoned"      # clause 6: expected_odometer_status='abandoned'
# defensive (NOT predicate clauses):
REAP_UNPROBEABLE = "unprobeable"    # probe returned no row — poison-pill defense (advance seed anyway)
REAP_UNEXPLAINED = "unexplained"    # row present, no clause failed — predicate/classifier DIVERGENCE (bug signal)

# offer_history columns the reap probe fetches (Option-B scoped probe, C3). NOTE: more
# than the delta's loose "six" — the staleness + band clauses also need created_at,
# pickup_miles, trip_miles. The per-tick DRIVER params (reference_time, cumulative_miles,
# effective_last_move) are NOT offer columns — they come from the heartbeat.
PROBE_COLUMNS = (
    "id", "created_at", "actual_pickup_at", "actual_dropoff_at",
    "expected_pickup_distance", "expected_dropoff_distance", "expected_odometer_status",
    "pickup_miles", "trip_miles",
)

KEYFRAME_EVENT_THRESHOLD = 50                       # C5: fire keyframe at >= 50 events since last
KEYFRAME_TIME_THRESHOLD = datetime.timedelta(minutes=15)   # OR >= 15 min, whichever first


def classify_reap(row, *, reference_time, cumulative_miles, effective_last_move):
    """Label WHY an offer left the live set, mirroring the 6 predicate clauses in
    precedence order (most-definitive first). Observability only — see module note.

    Args:
        row: the probed offer_history row (dict of PROBE_COLUMNS), or None.
        reference_time / cumulative_miles / effective_last_move: per-tick DRIVER params
            (the same values fed to live_offer_predicate_params this tick). Not offer cols.

    Returns: a reason string (REAP_BAND carries a ':pickup'|':dropoff' leg suffix).
    """
    if row is None:
        return REAP_UNPROBEABLE                      # poison-pill defense (advance the seed)

    # clause 1 — terminated (dropoff fired; most definitive)
    if row.get("actual_dropoff_at") is not None:
        return REAP_TERMINATED
    # clause 6 — explicitly abandoned (§9.9)
    if row.get("expected_odometer_status") == ODOMETER_STATUS_ABANDONED:
        return REAP_ABANDONED

    created_at = row.get("created_at")
    if created_at is not None and reference_time is not None:
        # clause 2 — causality (offer in the future; live-impossible but captured, C1)
        if created_at > reference_time:
            return REAP_CAUSALITY
        # clause 3 — abandonment ceiling (older than 4h)
        if created_at <= reference_time - datetime.timedelta(hours=GC_ABANDONMENT_CEILING_HOURS):
            return REAP_CEILING
        # clause 4 — odometer staleness (driver frozen >= freeze AND offer predates the freeze)
        freeze = datetime.timedelta(minutes=GC_ODOMETER_FREEZE_MINUTES)
        if (effective_last_move is not None
                and effective_last_move < reference_time - freeze
                and created_at <= reference_time - freeze):
            return REAP_STALENESS

    # clause 5 — band overshoot, UPPER edge only (per leg), reusing the single-owner band
    if cumulative_miles is not None:
        if row.get("actual_pickup_at") is None:       # pickup leg
            band = odometer_band(row.get("expected_pickup_distance"), row.get("pickup_miles"))
            leg = "pickup"
        else:                                          # dropoff leg
            band = odometer_band(row.get("expected_dropoff_distance"), row.get("trip_miles"))
            leg = "dropoff"
        if band is not None:
            center, tolerance = band
            if float(cumulative_miles) > center + tolerance:
                return f"{REAP_BAND}:{leg}"

    # The predicate reaped it but no clause we mirror failed — a real divergence
    # (predicate changed and the classifier drifted, or a clause we don't model).
    # Surface it loudly rather than silently mis-label. The clause-mapping test guards this.
    return REAP_UNEXPLAINED


def compute_diff(prior_ids, current_ids):
    """Pure set diff. Returns (entered, left) as sorted lists for determinism."""
    prior = set(prior_ids or ())
    current = set(current_ids or ())
    return sorted(current - prior), sorted(prior - current)


def probe_reaped_offers(cur, driver_id, left_ids):
    """Option-B scoped probe (C3): ONE query for the dropped ids' attribution columns.
    Fires only on a reap tick. Returns {offer_id(str): row(dict)}. The reaped offer's
    offer_history row is never deleted (C3), so it is readable post-reap."""
    if not left_ids:
        return {}
    cols = ", ".join(PROBE_COLUMNS)
    cur.execute(
        f"SELECT {cols} FROM app_private.offer_history "
        f"WHERE id::text = ANY(%s) "
        f"  AND decision_log_id IN (SELECT id FROM app_private.decision_log WHERE driver_id = %s)",
        (list(left_ids), driver_id),
    )
    return {str(r["id"]): r for r in cur.fetchall()}


def _matcher_snapshot(matches):
    """Compact prior-comparison state: {top_candidate_offer_id, confidence_tier}.
    Tier edges INCLUDE the 0.40 floor so a floor-cross is a tier change (C-§6.2)."""
    top = matches[0] if matches else None
    conf = top.confidence if top else 0.0
    # tiers: below-floor | floor-band | high. The 0.40 floor is a tier edge.
    if conf >= 0.70:
        tier = "high"
    elif conf >= 0.40:
        tier = "floor"
    else:
        tier = "below"
    return {
        "top_candidate_offer_id": (str(top.offer_id) if top else None),
        "confidence_tier": tier,
    }


def gather_ledger_events(
    *,
    prior_seed,            # dict from ledger_state, or None (cold start)
    current_ids,           # set/list of live offer_ids this tick (queue_offer_ids)
    current_status,        # {offer_id: {status,in_band,picked_up}} for the keyframe snapshot
    reap_rows,             # {offer_id: probe row} for left ids (from probe_reaped_offers)
    reference_time, cumulative_miles, effective_last_move,   # per-tick driver params
    matches,               # WAI matches (matcher inflection)
    executed_actions,      # dispatch actions this tick (PUDO events)
    arrest_started_at, arrest_counter_s,   # from the :1978 RETURNING
    cluster,               # diagnostics.cluster (or None)
    cadence_target_hz,     # this tick's cadence
    now,                   # reference_time again (UTC); kept explicit for keyframe stamping
):
    """PURE: assemble (events, new_seed) from in-hand tick state. No DB I/O — the probe
    already ran; this only classifies + assembles. The orchestrator emits the events and
    writes new_seed inside the C4 savepoint.

    Cold start (prior_seed is None / first post-migration tick): emit ONE drive-start
    keyframe, set seed := current, emit NO entered / NO reaps (an empty prior must not
    fabricate "all offers entered").
    """
    events = []
    current_set = set(current_ids or ())
    cold_start = prior_seed is None
    prior_count = int((prior_seed or {}).get("keyframe_count", 0))
    last_keyframe_at = (prior_seed or {}).get("last_keyframe_at")
    # ledger_state is jsonb → last_keyframe_at round-trips as an ISO STRING; parse it
    # back to a tz-aware datetime for the time-threshold comparison below.
    if isinstance(last_keyframe_at, str):
        last_keyframe_at = datetime.datetime.fromisoformat(last_keyframe_at)
    matcher_snap = _matcher_snapshot(matches)

    if not cold_start:
        prior_ids = (prior_seed or {}).get("last_queue_snapshot_ids", [])
        entered, left = compute_diff(prior_ids, current_set)
        for oid in entered:
            events.append({"event_type": "offer_entered_queue", "offer_id": oid,
                           "queue_delta": {"entered": [oid]}})
        for oid in left:
            reason = classify_reap(
                reap_rows.get(oid),
                reference_time=reference_time, cumulative_miles=cumulative_miles,
                effective_last_move=effective_last_move,
            )
            events.append({"event_type": "offer_left_queue", "offer_id": oid,
                           "queue_delta": {"left": [{"offer_id": oid, "reason": reason}]}})

        # matcher inflection: top-candidate change OR tier cross
        prior_matcher = (prior_seed or {}).get("last_matcher_snapshot") or {}
        if (matcher_snap["top_candidate_offer_id"] != prior_matcher.get("top_candidate_offer_id")
                or matcher_snap["confidence_tier"] != prior_matcher.get("confidence_tier")):
            events.append({"event_type": "matcher_eval",
                           "offer_id": matcher_snap["top_candidate_offer_id"],
                           "matcher_snapshot": matcher_snap})

        # PUDO events from executed actions (action class -> event)
        for ev in _pudo_events(executed_actions):
            events.append(ev)

    # keyframe decision (counter reset-to-0, NOT mod-50; keyframe ordered LAST in batch)
    new_count = (0 if cold_start else prior_count) + (0 if cold_start else len(events))
    fire_keyframe = (
        cold_start
        or new_count >= KEYFRAME_EVENT_THRESHOLD
        or (last_keyframe_at is not None and now is not None
            and now - last_keyframe_at >= KEYFRAME_TIME_THRESHOLD)
    )
    if fire_keyframe:
        events.append({"event_type": "keyframe",
                       "queue_snapshot": {"offers": current_status, "ids": sorted(current_set)}})
        new_count = 0
        new_keyframe_at = now
    else:
        new_keyframe_at = last_keyframe_at

    new_seed = {
        "last_queue_snapshot_ids": sorted(current_set),
        "last_matcher_snapshot": matcher_snap,
        "keyframe_count": new_count,
        "last_keyframe_at": (new_keyframe_at.isoformat() if isinstance(new_keyframe_at, datetime.datetime) else new_keyframe_at),
    }
    return events, new_seed


def _pudo_events(executed_actions):
    """Map executed dispatch actions to PUDO ledger events. Imported lazily (action
    classes live in dispatch.py — verified, not assumed)."""
    from dispatch import (
        FirePickup, FireDropoff, FirePickupObservation, FireDropoffObservation, ClearNarrative,
    )
    out = []
    for a in (executed_actions or []):
        if isinstance(a, (FirePickup, FirePickupObservation)):
            out.append({"event_type": "pickup_detected", "offer_id": str(getattr(a, "offer_id", None)),
                        "payload": {"observation": isinstance(a, FirePickupObservation)}})
        elif isinstance(a, (FireDropoff, FireDropoffObservation)):
            out.append({"event_type": "dropoff_detected", "offer_id": str(getattr(a, "offer_id", None)),
                        "payload": {"observation": isinstance(a, FireDropoffObservation)}})
        elif isinstance(a, ClearNarrative):
            out.append({"event_type": "unbind", "offer_id": str(getattr(a, "offer_id", None))})
    return out


def emit_event(cur, driver_id, ev, *, event_time=None, lat=None, lng=None,
               gps_accuracy_m=None, cumulative_miles=None, cluster_id=None, arrest_id=None):
    """The SOLE ledger writer: one append-only INSERT. Does NOT swallow — callers own the
    savepoint + swallow (emit_batch for the heartbeat batch; the /contest/label endpoint
    for ground_truth_tap). `event_time` defaults to the column's now() (system events);
    pass it explicitly (the device tap-time) for a ground_truth_tap so the row sits at the
    moment the human marked the PUDO, consistent with how tap-vs-detection has been analyzed."""
    def _j(v):
        return json.dumps(v) if v is not None else None
    cols = ["driver_id", "event_type", "offer_id", "cluster_id", "arrest_id",
            "lat", "lng", "gps_accuracy_m", "cumulative_miles",
            "queue_snapshot", "queue_delta", "matcher_snapshot", "payload", "summary"]
    vals = [driver_id, ev["event_type"], ev.get("offer_id"), cluster_id, arrest_id,
            lat, lng, gps_accuracy_m, cumulative_miles,
            _j(ev.get("queue_snapshot")), _j(ev.get("queue_delta")),
            _j(ev.get("matcher_snapshot")), _j(ev.get("payload")), ev.get("summary")]
    if event_time is not None:        # else the column DEFAULT now() (§II) applies
        cols.append("event_time")
        vals.append(event_time)
    cur.execute(
        f"INSERT INTO app_private.event_ledger ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(vals))})",
        vals,
    )


def read_ledger_seed(cur, driver_id):
    """Dedicated cheap PK read of the diff-seed at LOAD (uncoupled from
    compute_effective_last_move per §VIII). Returns the ledger_state dict or None."""
    cur.execute(
        "SELECT ledger_state FROM app_private.driver_trip_state WHERE driver_id = %s",
        (driver_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return row["ledger_state"]   # jsonb -> dict (or None on cold start)


def advance_ledger_seed(cur, driver_id, new_seed):
    """Write the diff-seed INSIDE the C4 batch savepoint (only on emitting ticks).
    The :1978 authoritative UPDATE never touches ledger_state, so this is the sole
    writer of it. HOT-eligible (driver_trip_state has only the PK index on driver_id)."""
    cur.execute(
        "UPDATE app_private.driver_trip_state SET ledger_state = %s::jsonb WHERE driver_id = %s",
        (json.dumps(new_seed), driver_id),
    )


def emit_batch(cur, driver_id, events, new_seed, *, ctx=None):
    """The C4 batched savepoint — the SOLE thing the wiring calls. All emits AND the
    seed-advance run atomically in ONE savepoint on the heartbeat connection, with a
    BATCH-level swallow (NOT per-emit). On any failure, ROLLBACK TO the savepoint reverts
    the emits AND the seed-advance together — so the next tick re-diffs the un-advanced
    seed and retries (no silent reap-loss, no duplicate; single connection ⇒ nothing
    partial separately committed). The parent's authoritative writes precede this
    savepoint and are untouched. Best-effort: a ledger failure never propagates (§VIII).

    Does NOT commit — post_heartbeat's terminal conn.commit() (:2406) owns that. `ctx`
    carries per-tick fields (lat/lng/gps_accuracy_m/cumulative_miles) passed to each emit.
    """
    if not events:
        return
    try:
        cur.execute("SAVEPOINT ledger_batch")
        for ev in events:
            emit_event(cur, driver_id, ev, **(ctx or {}))
        advance_ledger_seed(cur, driver_id, new_seed)
        cur.execute("RELEASE SAVEPOINT ledger_batch")
    except Exception as e:
        try:
            cur.execute("ROLLBACK TO SAVEPOINT ledger_batch")
        except Exception:
            pass
        log.warning("[event_ledger] emit batch failed (swallowed; seed not advanced): %s", e)
