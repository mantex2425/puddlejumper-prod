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

from pudo_types import Offer

log = logging.getLogger(__name__)

# GC-window tuning constants. Reconciled to canonical Houston Tax values
# in sub-commit 1c.1.1; previously drifted from driver_heartbeat's
# production-validated values despite a (now-removed) docstring claiming
# they were lifted verbatim. After Commit 1's caller-migration step,
# driver_heartbeat imports these from here rather than duplicating them.
GC_NULL_PICKUP_MIN = 15   # Default pickup_minutes when offer_history.pickup_minutes IS NULL
GC_NULL_TRIP_MIN = 30     # Default trip_minutes when offer_history.trip_minutes IS NULL
# Houston Tax: 25% dynamic buffer on (pickup_minutes + trip_minutes) sized
# to keep an offer live in the queue while the driver waits out real
# Houston-area traffic (Sienna Pkwy, McKeever Rd, the Arcola crawl).
# Tuned from production driving data; not arbitrary. If retuning, log the
# rationale in CANONICAL_RULES.md or the relevant phase closeout — this
# value drives queue retention semantics, not just a magic constant.
GC_BUFFER_MULT = 1.25
GC_MIN_MINUTES = 15       # Floor: even a 1-minute errand stays live for this long
GC_MAX_MINUTES = 240      # Ceiling: cap the airport-run window to 4 hours


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

    def snapshot(self, cur) -> QueueSnapshot:
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
        and emit a WARNING tagged INVARIANT_VIOLATION carrying the full
        forensic payload (driver_id, stale bound_offer_id, sorted queue
        offer_ids, queue size). The DB row is NOT corrected here — the
        next bind() or unbind() naturally overwrites it. Self-healing on
        read keeps this module read-only-by-default; only the dispatch
        wiring layer mutates the column.

        Raises:
            RuntimeError: if no target_spec_builder was supplied at
                construction time. Callers that don't need full Offer
                objects should use `offer_ids_only()` and
                `bound_offer_id()` instead.
        """
        offers = self._project_offers(cur)
        raw_bound = self._select_bound_offer_id(cur)

        # Apply L-19 invariant.
        if raw_bound is not None and raw_bound not in {o.offer_id for o in offers}:
            sorted_ids = sorted(o.offer_id for o in offers)
            log.warning(
                "[driver_queue] INVARIANT_VIOLATION: bound_offer_id points "
                "outside queue. driver_id=%s bound_offer_id=%s "
                "queue_size=%d queue_offer_ids=[%s]. Returning None for "
                "hint; next bind/unbind will correct the DB pointer.",
                self.driver_id,
                raw_bound,
                len(offers),
                ",".join(sorted_ids),
            )
            return QueueSnapshot(offers=offers, bound_offer_id=None)

        return QueueSnapshot(offers=offers, bound_offer_id=raw_bound)

    def offers(self, cur) -> tuple[Offer, ...]:
        """Just the queue projection. For non-heartbeat callers (replay,
        scenarios) that need full Offer objects but don't need the bound
        hint or the L-19 invariant. Requires a target_spec_builder.

        Raises:
            RuntimeError: if no target_spec_builder was supplied at
                construction time.
        """
        return self._project_offers(cur)

    def offer_ids_only(self, cur) -> tuple[str, ...]:
        """Project just the queue's offer_ids — no coord building, no
        TargetSpec construction. For monitor/status/forensic callers that
        only need to know "which offers are live for this driver right
        now." Does NOT require a target_spec_builder.

        Same GC math as the full projection.
        """
        cur.execute("""
            SELECT id::text AS offer_id
            FROM app_private.offer_history
            WHERE decision_log_id IN (
                SELECT id FROM app_private.decision_log WHERE driver_id = %s
            )
              AND actual_dropoff_at IS NULL
              AND created_at + (
                    LEAST(
                        GREATEST(
                            (COALESCE(pickup_minutes, %s) + COALESCE(trip_minutes, %s)) * %s,
                            %s
                        ),
                        %s
                    ) * INTERVAL '1 minute'
                  ) > NOW()
            ORDER BY created_at DESC
        """, (
            self.driver_id,
            GC_NULL_PICKUP_MIN, GC_NULL_TRIP_MIN,
            GC_BUFFER_MULT,
            GC_MIN_MINUTES,
            GC_MAX_MINUTES,
        ))
        return tuple(r['offer_id'] for r in cur.fetchall())

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

    def _project_offers(self, cur) -> tuple[Offer, ...]:
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

        cur.execute("""
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
            FROM app_private.offer_history
            WHERE decision_log_id IN (
                SELECT id FROM app_private.decision_log WHERE driver_id = %s
            )
              AND actual_dropoff_at IS NULL
              AND created_at + (
                    LEAST(
                        GREATEST(
                            (COALESCE(pickup_minutes, %s) + COALESCE(trip_minutes, %s)) * %s,
                            %s
                        ),
                        %s
                    ) * INTERVAL '1 minute'
                  ) > NOW()
            ORDER BY created_at DESC
        """, (
            GC_NULL_PICKUP_MIN, GC_NULL_TRIP_MIN,    # SELECT raw_min COALESCEs
            self.driver_id,                           # FK lookup
            GC_NULL_PICKUP_MIN, GC_NULL_TRIP_MIN,    # WHERE coalesces (must duplicate;
                                                      #   PG can't reuse SELECT alias here)
            GC_BUFFER_MULT,
            GC_MIN_MINUTES,
            GC_MAX_MINUTES,
        ))

        offers: list[Offer] = []
        for o in cur.fetchall():
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