"""DriverQueue — the projection of offer_history into a workload queue.

Per SIMPLIFIED_ARCHITECTURE.md §3 and §6: the queue is the GC-survivor set
of offer_history rows for a driver — every offer the driver has SEEN that
has not been completed (actual_dropoff_at IS NULL) AND has not aged out of
its per-offer GC window (the "Houston Tax").

`bound_offer_id` is a *hint*, not authoritative state. It witnesses which
offer FirePickup most recently fired against, used by dispatch.py to
disambiguate Case A (fresh pickup) from Case G (pickup re-match). Reading
it is cheap and non-authoritative; writing it happens via bind()/unbind()
at dispatch wiring time, or via force_bind() from test endpoints.

Invariant (enforced on read in `snapshot`): bound_offer_id IS NULL OR
bound_offer_id ∈ offers. Violations are logged at WARNING tagged
INVARIANT_VIOLATION and self-heal — the snapshot returns None for the
hint, the next bind() or unbind() writes the corrected pointer. See
L-19 in SESSION_PROTOCOL.md for the failure mode this invariant
addresses.

Module imports the canonical `Offer` dataclass from pudo_types — the
Source of Truth for shared data shapes. DriverQueue is the Workload
Manager; Offer is the Work Item. Consumers may import Offer from
pudo_types directly or transitively via driver_queue (re-exported
for convenience and for backward compatibility with sub-commit 1a
callers).

Architecture: TargetSpec construction is injected via a builder callable
supplied at DriverQueue construction time. This keeps the queue module
free of geocoding baggage while still delivering fully-formed Offer
objects to consumers that want them. Lightweight consumers (monitor,
status, drive_review) construct DriverQueue without a builder and use
`offer_ids_only()` / `bound_offer_id()` / `bind()` / `unbind()`; calling
`snapshot()` or `offers()` without a builder raises a clear error.

Standard compliance:
  §I  recon-first      — query shapes lifted from _project_queue verbatim
  §II canonical coords — no coord operations in this module
  §III UTC time         — no time-window math here; GC math stays in SQL
  §V  binding integrity — bind/unbind idempotency; INVARIANT_VIOLATION
                          warnings carry full forensic payload
                          (driver_id, bound_offer_id, queue offer_ids,
                          size) for instant audit
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from pudo_types import Offer, ODOMETER_BAND_NOISE_FLOOR_MI

log = logging.getLogger(__name__)

# GC-window tuning constants. Reconciled to canonical Houston Tax values
# in sub-commit 1c.1.1; previously drifted from driver_heartbeat's
# production-validated values despite a (now-removed) docstring claiming
# they were lifted verbatim. After Commit 1's caller-migration step,
# driver_heartbeat imports these from here rather than duplicating them.
GC_NULL_PICKUP_MIN = 15   # Default pickup_minutes when offer_history.pickup_minutes IS NULL
GC_NULL_TRIP_MIN = 30     # Default trip_minutes when offer_history.trip_minutes IS NULL

# Distance-axis defaults — mirror the time-axis pattern. Used when
# pickup_miles / trip_miles are NULL on offer_history rows.
# GC_NULL_PICKUP_MI / GC_NULL_TRIP_MI / GC_MIN_DIST_MI / GC_MAX_DIST_MI
# RETIRED 2026-06-05 (Step 6 piece i): the distance-band CASE that consumed
# them was replaced by the per-leg odometer band. The 2.0-mi floor is now the
# single-owner ODOMETER_BAND_NOISE_FLOOR_MI in pudo_types (superseding
# GC_MIN_DIST_MI). The 1.25 buffer / 50-mi cap / 4.0+8.0 miles fallbacks are
# gone — the band has no upper cap (a 235-mi offer stays live) and NULL leg
# distance is permissive, not fallback-to-a-guess. See ERRATUM §4.
# Houston Tax: 25% dynamic buffer on (pickup_minutes + trip_minutes) sized
# to keep an offer live in the queue while the driver waits out real
# Houston-area traffic (Sienna Pkwy, McKeever Rd, the Arcola crawl).
# Tuned from production driving data; not arbitrary. If retuning, log the
# rationale in CANONICAL_RULES.md or the relevant phase closeout — this
# value drives queue retention semantics, not just a magic constant.
# GC_BUFFER_MULT RETIRED 2026-06-05 (Step 6 piece i): the 25% trip-distance
# buffer was the Houston-Tax distance ceiling, superseded by the per-leg
# 0.15-of-leg-distance band (ODOMETER_BAND_TOLERANCE_PCT in pudo_types).
GC_MIN_MINUTES = 15       # Floor: even a 1-minute errand stays live for this long
GC_MAX_MINUTES = 240      # Ceiling: cap the airport-run window to 4 hours

# §XIV.H Odometer-Staleness Gate (2026-05-19): replaces the per-trip time
# horizon as the offer-liveness time axis. Offers are reaped when the
# driver's cumulative_miles has been frozen against reference_time for at
# least this many minutes AND the offer existed before the freeze began.
# 30 minutes sits cleanly above legitimate mid-ride stops (pickup waits,
# drive-thru, traffic) and below shift-end abandonment. Tunable from
# drive data — log rationale in CANONICAL_RULES.md if retuning.
GC_ODOMETER_FREEZE_MINUTES = 30

# §XIV.H Abandonment Ceiling (2026-05-19): hard cap on how long ANY
# offer can stay alive after creation. Preserves the GC_MAX_MINUTES
# = 240 (4 hour) cap that was buried inside the pre-P10 time
# horizon's clamp — that ceiling was always doing legitimate
# abandonment work, separate from the per-trip wait math we deleted.
#
# Catches cross-shift staleness (yesterday's never-fired offers),
# Android cumulative_miles accumulator resets between sessions,
# and any offer that aged out without firing pickup or dropoff.
# Houston→Austin (~3 hours) fits comfortably. Airport runs (~1 hour)
# unaffected.
GC_ABANDONMENT_CEILING_HOURS = 4


# =============================================================================
# Builder type — the seam between queue and geocoding
# =============================================================================

# A TargetSpecBuilder takes (address, lat, lng) — exactly the shape of
# offer_history.{pickup_address, pickup_lat, pickup_lng} (and the
# dropoff trio) — and returns a TargetSpec or None if the offer's
# geocode is unbuildable. The canonical implementation is
# driver_heartbeat._bucket_to_target_spec.
#
# We type the return as `object` here rather than importing TargetSpec
# from where_am_i to avoid a hard cycle and to keep this module a leaf
# in the dependency graph.
TargetSpecBuilder = Callable[[Optional[str], Optional[float], Optional[float]], Optional[object]]


# =============================================================================
# Offer — the Work Item (imported from pudo_types)
# =============================================================================
#
# Offer is imported at module-top from pudo_types, the Source of Truth
# for shared data shapes (per project convention). The dataclass has
# fields: offer_id (str), accepted_at (datetime), pickup (TargetSpec),
# dropoff (TargetSpec). The accepted_at field is sourced from
# offer_history.created_at — the row-creation moment, which serves as
# the "offer-seen" anchor for the Memory Eye in where_am_i.py
# regardless of verdict. See pudo_types.Offer for the full docstring.


# =============================================================================
# QueueSnapshot — the immutable per-tick view
# =============================================================================

@dataclass(frozen=True)
class QueueSnapshot:
    """Immutable view of the queue as of one heartbeat tick.

    `offers` and `bound_offer_id` are jointly consistent — the invariant
    has been applied. If `bound_offer_id` is non-None, it is guaranteed
    to appear as some `offer.offer_id` in `offers`.

    A `bound_offer_id` of None means either (a) no pickup has fired since
    the last dropoff (the natural pre-pickup state) OR (b) the pointer
    pointed at an offer that aged out of the queue and was self-healed
    on read. The two are indistinguishable from the snapshot alone;
    consult the WARNING log tagged INVARIANT_VIOLATION to disambiguate.
    """
    offers: tuple[Offer, ...]
    bound_offer_id: Optional[str]

    @property
    def offer_ids(self) -> frozenset[str]:
        return frozenset(o.offer_id for o in self.offers)

    @property
    def is_empty(self) -> bool:
        return len(self.offers) == 0


# =============================================================================
# Atomic Liveness Predicate — exported, canonical
# =============================================================================
#
# Per CANONICAL_RULES Section IV addendum (2026-05-10): every production
# hot-path query against app_private.offer_history MUST apply this
# predicate. The predicate enforces a Time horizon (always) and a
# Distance horizon (when current_cumulative_miles is supplied AND the
# row has miles_at_offer_receipt). Distance axis gracefully degrades to
# time-only when either signal is NULL.
#
# Two consumers as of P0 patch (2026-05-10):
#   - DriverQueue._project_offers  (queue projection)
#   - driver_heartbeat._get_last_known_anchor_id  (TAD anchor)
#   - decisions.logger prev_offer SELECT  (compute_offer_expectations
#                                          anchor source)
#
# New consumers must use this predicate or document a CANONICAL_RULES
# justification for why the row's freshness is not relevant to the
# call site.

LIVE_OFFER_PREDICATE_SQL = """
    oh.actual_dropoff_at IS NULL
    -- Causality Guard (2026-05-12): an offer cannot be "live" before
    -- it exists. Production server-clock was silently safe because
    -- wall-clock is always >= created_at. Replay against a historical
    -- reference_time is not — without this clause, future offers leak
    -- into the live set. See Houston Playback.
    AND oh.created_at <= %s::timestamptz
    -- Abandonment Ceiling (2026-05-19): no offer can stay alive longer
    -- than this regardless of distance progress. Preserves the
    -- GC_MAX_MINUTES = 240 (4 hour) cap from the pre-P10 architecture,
    -- which was buried inside the deleted time-horizon's clamp. Covers
    -- cross-shift staleness (yesterday's never-fired offers), Android
    -- accumulator resets, and any offer that aged out without firing
    -- pickup or dropoff. Houston→Austin (~3 hours) fits comfortably
    -- under this ceiling. The IAH airport run (~66 min) is unaffected.
    AND oh.created_at > %s::timestamptz - INTERVAL '%s hours'
    -- Odometer Staleness Gate (2026-05-19): replaces per-trip time
    -- horizon. Three sub-clauses:
    --   1. last_odometer_move_at is tracked (else gate is permissive)
    --   2. driver frozen for >= GC_ODOMETER_FREEZE_MINUTES against
    --      reference_time
    --   3. offer existed before the freeze began (don't reap born-stale
    --      offers received during a stationary period)
    AND NOT (
        %s::timestamptz IS NOT NULL
        AND %s::timestamptz < %s::timestamptz - INTERVAL '%s minutes'
        AND oh.created_at <= %s::timestamptz - INTERVAL '%s minutes'
    )
    -- Odometer band (Step 6, ERRATUM 2026-06-05 §4). Per-leg, UPPER-EDGE
    -- only: this is the LIVENESS layer, so it reaps an offer that has
    -- OVERSHOT its band but NEVER reaps a not-yet-reached offer (the driver
    -- is still en route; the lower edge is the candidacy/wallet layer's
    -- concern in tad.py, not liveness). The band is the single-owner
    -- quantity defined in pudo_types.odometer_band; this SQL is hand-written
    -- to that formula and pinned to it by the equivalence test
    -- (test_band_clause_matches_primitive). NO bridge term (ERRATUM §2): the
    -- center is tad.py's expected_*_distance anchor, consumed as-is.
    --
    --   pickup  leg (actual_pickup_at IS NULL):
    --     center = expected_pickup_distance, width = max(0.15*pickup_miles, floor)
    --   dropoff leg (actual_pickup_at IS NOT NULL):
    --     center = expected_dropoff_distance, width = max(0.15*trip_miles, floor)
    --
    -- NULL-permissive (ERRATUM §4, ratified 2026-06-05; 0/448 NULL in 30d):
    -- NULL odometer, NULL center, or NULL leg-distance -> the branch is TRUE
    -- (offer stays live -> §5.5 deferred sentinel). Explicit IS NULL guards,
    -- not 3-valued-logic reliance. Replaces the old GC_NULL_*_MI fallback.
    AND (
        %s::numeric IS NULL
        OR (
            CASE
                WHEN oh.actual_pickup_at IS NULL THEN
                    -- pickup leg: permissive if no center or no leg distance
                    oh.expected_pickup_distance IS NULL
                    OR oh.pickup_miles IS NULL
                    OR %s::numeric <= oh.expected_pickup_distance
                       + GREATEST(0.15 * oh.pickup_miles, %s)
                ELSE
                    -- dropoff leg: permissive if no center or no leg distance
                    oh.expected_dropoff_distance IS NULL
                    OR oh.trip_miles IS NULL
                    OR %s::numeric <= oh.expected_dropoff_distance
                       + GREATEST(0.15 * oh.trip_miles, %s)
            END
        )
    )
"""


def _now():
    """Capture the current UTC time. Wrapping in a function makes it
    trivially mockable in tests and gives us a single audit point if
    we ever need to swap the clock source (e.g., for replay harnesses).
    """
    import datetime
    return datetime.datetime.now(datetime.timezone.utc)


def _is_offer_definitively_dead(cur, offer_id, reference_time=None):
    """Return a reason string if `offer_id` is definitively dead, else None.

    "Definitively dead" requires POSITIVE evidence from an existing
    offer_history row. Absence is NOT death — bind() can outrun
    offer_history INSERT in ingestion paths (notably the test_endpoints
    seed_offer flow), so a missing row is treated as transient.
    Ratified 2026-05-31 (see fix/stale-current-offer-id-reconciliation
    brief §3 clarification): "Drop the orphan branch — clear only on
    terminated or abandoned, both requiring positive evidence of death
    from an existing row. Absence-as-death races mid-ingestion."

    Return values (string reasons used in reconciliation log lines AND
    docs/CANONICAL_RULES.md §XVIII.A):
      "terminated" — actual_dropoff_at IS NOT NULL (trip fired dropoff)
      "abandoned"  — created_at older than GC_ABANDONMENT_CEILING_HOURS
                     against reference_time (4h cap, the same canonical
                     ceiling LIVE_OFFER_PREDICATE_SQL enforces)
      None         — row absent, OR row present and neither dead
                     condition holds (transient — DO NOT reconcile)

    Order is deterministic for log clarity (terminated → abandoned):
    a row could satisfy both clauses (old AND fired-dropoff); we report
    the more specific cause first. The two conditions are individually
    sufficient, never required together.

    Args:
        cur: psycopg2 cursor; RealDictCursor or anything where fetchone
            returns a dict-like with the queried columns.
        offer_id: offer_history.id (str-castable to bigint).
        reference_time: UTC datetime, optional. Defaults to _now().
            Pass an explicit value from replay harnesses to make the
            abandonment check deterministic against historical data.

    Used by DriverQueue.snapshot() to gate the active clearing of a
    stale driver_trip_state.current_offer_id pointer. See
    docs/CANONICAL_RULES.md §XVIII.A "Stale-pointer reconciliation"
    for the canonical specification.
    """
    if reference_time is None:
        reference_time = _now()

    cur.execute(
        """
        SELECT actual_dropoff_at, created_at
        FROM app_private.offer_history
        WHERE id = %s::bigint
        """,
        (offer_id,),
    )
    row = cur.fetchone()
    if row is None:
        # Absent — transient, NOT dead. Reconciliation MUST skip.
        # The bind/snapshot ingestion race window can leave a
        # current_offer_id pointer ahead of its offer_history row;
        # the next heartbeat after the INSERT commits will see the
        # row and route normally.
        return None
    if row["actual_dropoff_at"] is not None:
        return "terminated"
    import datetime
    ceiling = reference_time - datetime.timedelta(hours=GC_ABANDONMENT_CEILING_HOURS)
    if row["created_at"] < ceiling:
        return "abandoned"
    return None


def live_offer_predicate_params(current_cumulative_miles, reference_time, last_odometer_move_at=None):
    """Build the params tuple for LIVE_OFFER_PREDICATE_SQL.

    2026-05-12 Rule VII refactor: reference_time is REQUIRED. The
    predicate SQL no longer references NOW() server-side; the clock
    is explicit data. This makes LIVE_OFFER_PREDICATE_SQL a pure
    function — same inputs always produce the same answer regardless
    of when Postgres evaluates the query. Required for the Houston
    Playback test to anchor against historical drive data.

    Args:
        current_cumulative_miles: float or None. When None, the
            distance axis short-circuits to TRUE (time-only fallback).
            Production heartbeat path always supplies a value; test
            fixtures may pass None to bypass the distance check.
        reference_time: datetime, REQUIRED. The "now" against which
            the time horizon is evaluated. Production captures
            `datetime.now(timezone.utc)` once at the top of each
            heartbeat and threads through. Tests pass any UTC
            datetime to replay against historical moments.

    Returns:
        13-tuple to splice into the params list at the call site.
    """
    return (
        # Causality Guard (2026-05-12): reference_time bound to the
        # `oh.created_at <= %s::timestamptz` clause. Prevents future
        # offers from leaking into the live set during replay.
        reference_time,
        # Legacy Schema Safety Valve (2026-05-19): reference_time and
        # GC_ABANDONMENT_CEILING_HOURS bound to the `oh.created_at > ref - N hours`
        # clause. Reaps zombie rows from pre-receipt-capture schema (and
        # modern orphans where the receipt-write pipeline dropped).
        reference_time,
        GC_ABANDONMENT_CEILING_HOURS,
        # Odometer Staleness Gate (2026-05-19): four params for the
        # 3-sub-clause NOT(...) block — last_odometer_move_at twice
        # (NULL guard + age comparison), reference_time twice (age and
        # new-offer exemption), GC_ODOMETER_FREEZE_MINUTES twice (same
        # interval used for both clauses).
        last_odometer_move_at,                              # NULL guard
        last_odometer_move_at,                              # age comparison LHS
        reference_time,                                     # age comparison RHS
        GC_ODOMETER_FREEZE_MINUTES,                         # age interval
        reference_time,                                     # new-offer-exemption comparison RHS
        GC_ODOMETER_FREEZE_MINUTES,                         # new-offer-exemption interval
        # Odometer band (Step 6, ERRATUM §4). The new band clause references
        # the odometer THREE times — the outer NULL-guard, the pickup-leg
        # comparison, the dropoff-leg comparison — and the noise floor TWICE
        # (one per leg branch). Postgres cannot reference a param twice, so we
        # duplicate. ODOMETER_BAND_NOISE_FLOOR_MI is the single-owner floor
        # (pudo_types), superseding the retired GC_MIN_DIST_MI.
        current_cumulative_miles,   # outer NULL guard
        current_cumulative_miles,   # pickup-leg comparison
        ODOMETER_BAND_NOISE_FLOOR_MI,   # pickup-leg width floor
        current_cumulative_miles,   # dropoff-leg comparison
        ODOMETER_BAND_NOISE_FLOOR_MI,   # dropoff-leg width floor
    )


def _log_distance_cull_if_any(cur, driver_id, current_cumulative_miles,
                              reference_time, last_odometer_move_at,
                              method_label, live_count):
    """Emit a DEBUG line when the distance envelope reaps offers that
    otherwise cleared the temporal gates (causality + 4h ceiling +
    odometer-staleness). Provides permanent observability on what the
    distance axis is doing under live driving.

    Gated twice to keep the cost honest in a 3 s-cadence single-driver
    system:

      - Skipped entirely when current_cumulative_miles is None (the
        forensic/replay path can't cull on distance by construction).
      - Log line emitted ONLY when culled_count > 0; idle ticks with
        nothing in the envelope produce no log noise. The COUNT query
        still runs every active-drive call to observe the diff.

    Compares the caller's live_count (full predicate) against a
    temporal-only count produced by re-running LIVE_OFFER_PREDICATE_SQL
    with cum_miles=None — that NULL short-circuits the distance gate to
    TRUE while leaving every other gate evaluating against the real
    reference_time and last_odometer_move_at.
    """
    if current_cumulative_miles is None:
        return
    cur.execute(
        f"""
        SELECT COUNT(*)::int AS n
        FROM app_private.offer_history oh
        WHERE decision_log_id IN (
            SELECT id FROM app_private.decision_log WHERE driver_id = %s
        )
          AND {LIVE_OFFER_PREDICATE_SQL}
        """,
        (driver_id,) + live_offer_predicate_params(None, reference_time, last_odometer_move_at),
    )
    temporal_n = cur.fetchone()["n"]
    culled = temporal_n - live_count
    if culled > 0:
        log.debug(
            "[driver_queue] distance_envelope culled %d offer(s) "
            "method=%s driver_id=%s cum_miles=%s lom_at=%s "
            "temporal_pass=%d live=%d",
            culled, method_label, driver_id, current_cumulative_miles,
            last_odometer_move_at, temporal_n, live_count,
        )


# =============================================================================
# DriverQueue — the Workload Manager
# =============================================================================

class DriverQueue:
    """Owns the read+write surface for the GC-survivor queue and the
    `bound_offer_id` hint on driver_trip_state.

    Construct once per request/handler; pass the cursor for each
    operation. DriverQueue does NOT own the connection or transaction —
    callers retain that. DriverQueue is a query-shape repository, not
    a unit of work.

    Args:
        driver_id: the Firebase UID we're operating on behalf of.
        target_spec_builder: optional callable that turns
            (address, lat, lng) into a TargetSpec or None. Required for
            `snapshot()` and `offers()` (which build full Offer objects);
            unused by `offer_ids_only()`, `bound_offer_id()`, `bind()`,
            `unbind()`, `force_bind()`. Lightweight callers may omit it.
    """

    def __init__(
        self,
        driver_id: str,
        target_spec_builder: Optional[TargetSpecBuilder] = None,
    ) -> None:
        self.driver_id = driver_id
        self._build_target_spec = target_spec_builder

    # -------------------------------------------------------------------------
    # Reads
    # -------------------------------------------------------------------------

    def snapshot(self, cur, *, current_cumulative_miles, last_odometer_move_at) -> QueueSnapshot:
        """Project the queue and read the bound hint in one logical operation.

        Two SELECTs run on the caller's cursor. In Postgres READ COMMITTED
        isolation (the default) these can technically see different MVCC
        snapshots — but the only writer to driver_trip_state.current_offer_id
        for a given driver is this same heartbeat handler (or
        bind/unbind from manual confirm endpoints, which do not run
        concurrently with the heartbeat for the same driver in production).
        So no real race exists. The "atomic read" property is provided by
        the absence of concurrent writers, not by isolation level.

        Applies the L-19 invariant: if `bound_offer_id` is set but does
        NOT appear in the projected offer set, return None for the hint
        and reconcile the DB pointer when the bound offer is
        DEFINITIVELY DEAD per `_is_offer_definitively_dead`. Two
        outcomes:

          - Dead (terminated OR abandoned, per existing offer_history
            row): issue id-guarded `UPDATE driver_trip_state SET
            current_offer_id = NULL WHERE driver_id = %s AND
            current_offer_id = %s` and emit INFO. The id-guarded WHERE
            ensures a concurrent fresh bind() to a different offer
            cannot be clobbered. Ratified 2026-05-31 to fix the
            offer 8585 / offer 8657 class — pickup fired, dropoff
            never fired (lost-mode, GC-reaped dropoff leg), pointer
            dangled across shifts because "next bind/unbind" never
            arrived in lost-mode.

          - Not dead (offer is transient — row absent mid-ingestion,
            OR present but recent + no dropoff): emit the historical
            WARNING tagged INVARIANT_VIOLATION carrying the full
            forensic payload. DB row is NOT touched. Preserves the
            original L-19 self-heal semantics for genuinely transient
            cases. This is the closed-race version: a live offer that
            just hasn't projected yet (mid-snapshot, mid-bind, network
            blip) fails the dead predicate and is left alone.

        Either outcome returns `QueueSnapshot(bound_offer_id=None)` —
        the in-memory hint always self-heals; only the DB write
        differs between the two branches.

        See docs/CANONICAL_RULES.md §XVIII.A "Stale-pointer
        reconciliation" for the canonical specification of the
        dead predicate.

        Raises:
            RuntimeError: if no target_spec_builder was supplied at
                construction time. Callers that don't need full Offer
                objects should use `offer_ids_only()` and
                `bound_offer_id()` instead.
        """
        # 2026-05-30 bind-drift fix: snapshot() previously dropped
        # last_odometer_move_at silently when calling _project_offers,
        # leaving the matcher's odometer-staleness gate permissive
        # despite the heartbeat handler threading a real value through.
        # Both kwargs now forwarded explicitly. See
        # docs/RECON_MATCHER_EMPTY_CANDIDATES_2026-05-30.md §A.3 +
        # "secondary latent bug" note.
        offers = self._project_offers(
            cur,
            current_cumulative_miles=current_cumulative_miles,
            last_odometer_move_at=last_odometer_move_at,
        )
        raw_bound = self._select_bound_offer_id(cur)

        # Apply L-19 invariant.
        if raw_bound is not None and raw_bound not in {o.offer_id for o in offers}:
            sorted_ids = sorted(o.offer_id for o in offers)
            dead_reason = _is_offer_definitively_dead(cur, raw_bound)
            if dead_reason is not None:
                # 2026-05-31: actively reconcile the stale DB pointer.
                # The id-guarded WHERE makes the UPDATE idempotent AND
                # safe under concurrent bind() — if another transaction
                # bound a fresh offer_id between our _select_bound_offer_id
                # read above and this UPDATE, the WHERE current_offer_id=%s
                # clause fails to match and the UPDATE no-ops, preserving
                # the fresh bind. See brief §2 "definitively-dead predicate
                # closes the race by construction."
                cur.execute(
                    """
                    UPDATE app_private.driver_trip_state
                    SET current_offer_id = NULL
                    WHERE driver_id = %s
                      AND current_offer_id = %s
                    """,
                    (self.driver_id, raw_bound),
                )
                log.info(
                    "[driver_queue] reconciled stale current_offer_id: "
                    "driver_id=%s cleared_offer_id=%s reason=%s "
                    "queue_size=%d queue_offer_ids=[%s]",
                    self.driver_id,
                    raw_bound,
                    dead_reason,
                    len(offers),
                    ",".join(sorted_ids),
                )
            else:
                # Not definitively dead — leave the DB pointer alone.
                # Either the offer is mid-ingestion (row not yet in
                # offer_history) or genuinely recent + still-alive
                # but un-projected for some other reason. Either way,
                # clearing would be premature. Next heartbeat re-runs
                # the same predicate; if the row ever lands in a dead
                # state, the next snapshot reconciles.
                log.warning(
                    "[driver_queue] INVARIANT_VIOLATION: bound_offer_id points "
                    "outside queue but not definitively dead. driver_id=%s "
                    "bound_offer_id=%s queue_size=%d queue_offer_ids=[%s]. "
                    "Returning None for hint; DB pointer preserved (transient).",
                    self.driver_id,
                    raw_bound,
                    len(offers),
                    ",".join(sorted_ids),
                )
            return QueueSnapshot(offers=offers, bound_offer_id=None)

        return QueueSnapshot(offers=offers, bound_offer_id=raw_bound)

    def offers(self, cur, *, current_cumulative_miles, last_odometer_move_at) -> tuple[Offer, ...]:
        """Just the queue projection. For non-heartbeat callers (replay,
        scenarios) that need full Offer objects but don't need the bound
        hint or the L-19 invariant. Requires a target_spec_builder.

        Raises:
            RuntimeError: if no target_spec_builder was supplied at
                construction time.
        """
        # 2026-05-30 bind-drift fix (same as snapshot()): forward both
        # kwargs to _project_offers so the odometer-staleness gate
        # evaluates against caller-supplied state, not silently against
        # None. See docs/RECON_MATCHER_EMPTY_CANDIDATES_2026-05-30.md.
        return self._project_offers(
            cur,
            current_cumulative_miles=current_cumulative_miles,
            last_odometer_move_at=last_odometer_move_at,
        )

    def offer_ids_only(self, cur, *, current_cumulative_miles, last_odometer_move_at) -> tuple[str, ...]:
        """Project just the queue's offer_ids — no coord building, no
        TargetSpec construction. For monitor/status/forensic callers that
        only need to know "which offers are live for this driver right
        now." Does NOT require a target_spec_builder.

        Same GC math as the full projection.

        P0 fix 2026-05-10: refactored to use LIVE_OFFER_PREDICATE_SQL
        for source-textual identity with _project_offers (enforced by
        test_offer_ids_only_and_project_offers_share_where_clause).
        """
        reference_time = _now()
        cur.execute(f"""
            SELECT id::text AS offer_id
            FROM app_private.offer_history oh
            WHERE decision_log_id IN (
                SELECT id FROM app_private.decision_log WHERE driver_id = %s
            )
              AND {LIVE_OFFER_PREDICATE_SQL}
            ORDER BY created_at DESC
        """, (
            self.driver_id,
        ) + live_offer_predicate_params(current_cumulative_miles, reference_time, last_odometer_move_at))
        result = tuple(r['offer_id'] for r in cur.fetchall())
        _log_distance_cull_if_any(
            cur, self.driver_id, current_cumulative_miles,
            reference_time, last_odometer_move_at,
            method_label="offer_ids_only", live_count=len(result),
        )
        return result

    def bound_offer_id(self, cur) -> Optional[str]:
        """Just the hint. For callers (manual confirm, decisions/router)
        that need the pointer without re-projecting the queue.

        Does NOT apply the L-19 invariant — caller is responsible for
        treating the result as advisory unless they also call offers()
        or snapshot(). Manual confirm endpoints follow the pointer to
        find the offer's coords; if the pointer is stale they fail
        naturally on the offer_history lookup, which is the desired
        behavior (loud failure on a stale manual nail attempt).
        """
        return self._select_bound_offer_id(cur)

    # -------------------------------------------------------------------------
    # Writes
    # -------------------------------------------------------------------------

    def bind(self, offer_id: str, cur) -> None:
        """Set bound_offer_id to `offer_id`. Wired by dispatch's FirePickup
        execution. Idempotent: re-binding to the already-bound offer is
        a silent no-op (we still issue the UPDATE for simplicity; Postgres
        treats it as a normal write but no row changes value).

        Caller is responsible for ensuring `offer_id` is queue-eligible.
        Production callers always do (FirePickup is a dispatch action
        derived from a queue match). Test endpoints that want to bypass
        this contract use force_bind().
        """
        cur.execute("""
            UPDATE app_private.driver_trip_state
            SET current_offer_id = %s
            WHERE driver_id = %s
        """, (offer_id, self.driver_id))

    def unbind(self, cur, *, clear_heartbeat: bool = False) -> None:
        """Clear bound_offer_id. Wired by dispatch's FireDropoff execution
        and by /api/v1/test/reset_driver. Idempotent: clearing an
        already-NULL pointer is a normal write that changes no values.

        Args:
            cur: psycopg2 cursor.
            clear_heartbeat: if True, ALSO clears heartbeat and
                heartbeat_at columns on the same UPDATE. Used only by
                test infrastructure that resets a driver's full row
                state. Production callers (FireDropoff) use the default
                False — heartbeat fields are owned by the heartbeat
                handler's own write path, not the dispatch wiring.

        Logging:
            Default (clear_heartbeat=False): silent. FireDropoff fires
            this on every dropoff (~thousands/day in production); we
            don't want that volume in INFO logs.
            clear_heartbeat=True: emits INFO with driver_id and the
            "+heartbeat" qualifier so forensic timeline reconstruction
            can distinguish a normal dropoff unbind from a test reset.
        """
        if clear_heartbeat:
            log.info(
                "[driver_queue] unbind+heartbeat driver_id=%s",
                self.driver_id,
            )
            cur.execute("""
                UPDATE app_private.driver_trip_state
                SET current_offer_id = NULL,
                    heartbeat = NULL,
                    heartbeat_at = NULL
                WHERE driver_id = %s
            """, (self.driver_id,))
        else:
            cur.execute("""
                UPDATE app_private.driver_trip_state
                SET current_offer_id = NULL
                WHERE driver_id = %s
            """, (self.driver_id,))

    # -------------------------------------------------------------------------
    # Test surface
    # -------------------------------------------------------------------------

    def force_bind(self, offer_id: str, cur) -> None:
        """Test-only. Same effect as bind() but with INSERT-or-UPDATE
        semantics (handles the case where driver_trip_state has no row
        for this driver yet) and an INFO log so calls are traceable in
        test output. Intended for endpoints that write the offer_history
        row and the bound pointer in the same transaction (e.g.
        /api/v1/test/seed_offer) where same-tx visibility makes a
        pre-bind queue check unreliable.

        Production code paths use bind(). If you find yourself reaching
        for force_bind() outside test_endpoints.py, stop and reconsider —
        L-19 was caused by exactly this pattern (pointer written without
        a corresponding queue-eligible offer).
        """
        log.info(
            "[driver_queue] force_bind driver_id=%s offer_id=%s",
            self.driver_id, offer_id,
        )
        cur.execute("""
            INSERT INTO app_private.driver_trip_state (driver_id, current_offer_id)
            VALUES (%s, %s)
            ON CONFLICT (driver_id) DO UPDATE
            SET current_offer_id = EXCLUDED.current_offer_id
        """, (self.driver_id, offer_id))

    # -------------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------------

    def _project_offers(self, cur, current_cumulative_miles=None, last_odometer_move_at=None) -> tuple[Offer, ...]:
        """The full GC-survivor projection. Query lifted from
        driver_heartbeat._project_queue (commit 7fe4391-era body) and
        unchanged here — recon-first, no behavior change in Commit 1.

        Per-offer GC window (in minutes):
            raw_min    = COALESCE(pickup_minutes, GC_NULL_PICKUP_MIN)
                       + COALESCE(trip_minutes,   GC_NULL_TRIP_MIN)
            window_min = LEAST(GREATEST(raw_min * GC_BUFFER_MULT,
                                        GC_MIN_MINUTES),
                               GC_MAX_MINUTES)
            live       = (NOW() - created_at) < window_min minutes

        Offers with unbuildable geocodes (builder returns None for either
        pickup or dropoff) are logged at WARNING and skipped — same
        behavior as _project_queue today.

        Raises:
            RuntimeError: if no target_spec_builder was supplied at
                construction time.
        """
        if self._build_target_spec is None:
            raise RuntimeError(
                "DriverQueue.snapshot() / offers() require a "
                "target_spec_builder. Construct DriverQueue with one, or "
                "use offer_ids_only() / bound_offer_id() if you don't "
                "need full Offer objects."
            )

        reference_time = _now()
        cur.execute(f"""
            SELECT
                id, pickup_address, dropoff_address,
                pickup_lat, pickup_lng,
                dropoff_lat, dropoff_lng,
                created_at,
                pickup_miles, trip_miles,
                pickup_minutes, trip_minutes,
                leg_start_cumulative_miles_pickup,
                leg_start_cumulative_miles_dropoff,
                COALESCE(pickup_minutes, %s) + COALESCE(trip_minutes, %s) AS raw_min
            FROM app_private.offer_history oh
            WHERE decision_log_id IN (
                SELECT id FROM app_private.decision_log WHERE driver_id = %s
            )
              AND {LIVE_OFFER_PREDICATE_SQL}
            ORDER BY created_at DESC
        """, (
            GC_NULL_PICKUP_MIN, GC_NULL_TRIP_MIN,    # SELECT raw_min COALESCEs
            self.driver_id,                           # FK lookup
        ) + live_offer_predicate_params(current_cumulative_miles, reference_time, last_odometer_move_at))

        rows = cur.fetchall()
        _log_distance_cull_if_any(
            cur, self.driver_id, current_cumulative_miles,
            reference_time, last_odometer_move_at,
            method_label="_project_offers", live_count=len(rows),
        )

        offers: list[Offer] = []
        for o in rows:
            pickup_spec = self._build_target_spec(
                o['pickup_address'], o['pickup_lat'], o['pickup_lng'])
            dropoff_spec = self._build_target_spec(
                o['dropoff_address'], o['dropoff_lat'], o['dropoff_lng'])
            if pickup_spec is None or dropoff_spec is None:
                log.warning(
                    "[driver_queue] offer %s has unbuildable geocode "
                    "(pickup_ok=%s dropoff_ok=%s) — excluded from queue",
                    o['id'], pickup_spec is not None, dropoff_spec is not None,
                )
                continue
            offers.append(Offer(
                offer_id=str(o['id']),
                accepted_at=o['created_at'],
                pickup=pickup_spec,
                dropoff=dropoff_spec,
                pickup_miles=(
                    float(o['pickup_miles']) if o.get('pickup_miles') is not None else None
                ),
                trip_miles=(
                    float(o['trip_miles']) if o.get('trip_miles') is not None else None
                ),
                leg_start_cumulative_miles_pickup=(
                    float(o['leg_start_cumulative_miles_pickup'])
                    if o.get('leg_start_cumulative_miles_pickup') is not None else None
                ),
                leg_start_cumulative_miles_dropoff=(
                    float(o['leg_start_cumulative_miles_dropoff'])
                    if o.get('leg_start_cumulative_miles_dropoff') is not None else None
                ),
                pickup_minutes=(
                    int(o['pickup_minutes']) if o.get('pickup_minutes') is not None else None
                ),
                trip_minutes=(
                    int(o['trip_minutes']) if o.get('trip_minutes') is not None else None
                ),
            ))
        return tuple(offers)

    def _select_bound_offer_id(self, cur) -> Optional[str]:
        cur.execute("""
            SELECT current_offer_id
            FROM app_private.driver_trip_state
            WHERE driver_id = %s
        """, (self.driver_id,))
        row = cur.fetchone()
        if not row:
            return None
        val = row['current_offer_id']
        return str(val) if val else None
