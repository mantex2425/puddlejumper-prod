# Out-of-band `app_private.offer_history` queries

Per **§XIV.H Out-of-band Exceptions** (`docs/CANONICAL_RULES.md:647`):
out-of-band scripts (replay, backtest, harvest, drive_review, ad-hoc
recon) may query `app_private.offer_history` without the canonical
`LIVE_OFFER_PREDICATE_SQL`. Each such call site must be documented
here with the reason for exception.

The bar: production hot-path code MUST go through the predicate.
Anything else — replay against historical reference_time, forensic
reconstruction at investigation time, debug breakouts that intentionally
want gates disabled — lands here.

---

## 2026-06-01 — Queue-no-reap recon (H1/H2 disambiguation)

**Investigator:** Andrew + Claude
**Recon doc:** `docs/RECON_QUEUE_NO_REAP_2026-06-01.md`
**Why out-of-band:** the entire purpose of the queries was to show what
the predicate returns under two *different* parameter regimes (NULL/NULL
vs. real per-tick state) against the same offer_history rows at the same
instant. Threading the canonical predicate via the production code path
would have observed only one regime (whichever the call site provides);
running the predicate text directly with explicit CTE-bound params let
us A/B the two paths in one psql session and prove the bug is at the
caller, not in the predicate.

**Bound parameters used:**

| Section | `cum_miles` | `last_odometer_move_at` | Notes |
|---|---|---|---|
| §4 monitor-path | NULL | NULL | mirrors `driver_status.py:188` caller |
| §5 heartbeat-path | 362.36 | 2026-06-01 18:14:28 UTC | mirrors `driver_heartbeat.py:1659` |
| §6 per-offer | 362.36 | 2026-06-01 18:14:28 UTC | gate-by-gate evaluation |

Constants pulled verbatim from `driver_queue.py`:
`GC_ABANDONMENT_CEILING_HOURS=4`, `GC_ODOMETER_FREEZE_MINUTES=30`,
`GC_NULL_PICKUP_MI=4.0`, `GC_NULL_TRIP_MI=8.0`, `GC_BUFFER_MULT=1.25`,
`GC_MIN_DIST_MI=2.0`, `GC_MAX_DIST_MI=50.0`.

**Predicate text fidelity:** the SQL inlined in the heredoc transcribes
`LIVE_OFFER_PREDICATE_SQL` (`driver_queue.py:183-237`) clause-for-clause.
Causality guard, abandonment ceiling, odometer-staleness 3-sub-clause
NOT-block, and the post-pickup/pre-pickup distance envelope are all
present and identical.

**Scope:** single driver (`UjT1hE9eBXh2q95aSZYOkzDJ8lo1`), single date
(2026-06-01), 13 rows. Read-only. No mutations.

---

## 2026-06-01 — Pickup-no-bind recon (H-A vs H-B disambiguation)

**Investigator:** Andrew + Claude
**Recon doc:** `docs/RECON_PICKUP_NOBIND_2026-06-01.md`
**Why out-of-band:** the recon reconstructed each pickup-class fire's
`alive_unpicked_offer_ids` set as-of that fire's timestamp, against
historical `offer_history` state — which is exactly the workload the
predicate is NOT designed for. `LIVE_OFFER_PREDICATE_SQL` evaluates
`oh.actual_pickup_at IS NULL` and `oh.actual_dropoff_at IS NULL` in
present tense; for replay, those clauses needed historical overrides
(`actual_pickup_at IS NULL OR ≥ reference_time`,
`actual_dropoff_at IS NULL OR > reference_time`). Going through the
production helper would have given the *current* set, not the
*as-of-fire* set.

**Bound parameters used:** reference_time = each PDC fire's
`created_at`; `cum_miles` taken from the closest preceding
`heartbeat_log` row; `last_odometer_move_at` treated as ≈ reference_time
(driver was actively moving up to arrest, so the 30-min staleness gate
was permissive — matches production behavior in active-drive moments).

**Predicate text fidelity:** SQL inlined in the heredoc transcribes
`LIVE_OFFER_PREDICATE_SQL` (`driver_queue.py:183-237`) clause-for-clause
except for the two historical-state overrides documented above.
Constants pulled verbatim: `GC_ABANDONMENT_CEILING_HOURS=4`,
`GC_NULL_PICKUP_MI=4.0`, `GC_NULL_TRIP_MI=8.0`, `GC_BUFFER_MULT=1.25`,
`GC_MIN_DIST_MI=2.0`, `GC_MAX_DIST_MI=50.0`.

**Scope:** single driver, single date, 7 pickup-class fire events + 13
offer_history rows + 4 PDC rows for §XVI.G distance computation.
Read-only. No mutations.
