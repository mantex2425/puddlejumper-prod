# Identity Crisis — Technical Debt (2026-05-08)

**Status:** Active technical debt. Surgical workaround applied (Option α / Alpha-Patch). Architectural realignment (Option β) deferred to a dedicated future sprint.
**Discovered during:** Phase 2c.2 Item 3d pre-author recon (2026-05-08).
**Workaround:** `tmp/apply_alpha_id_fix.sh` patches `_execute_action` to translate `offer_history.id` → `decision_log.id` at the schema boundary via subquery. See commit message of the alpha-patch commit.

---

## Summary

The system has two competing identifiers for an "offer":

- **`offer_history.id`** (`bigint`) — what `Offer.offer_id: str` actually carries throughout the Python layer (`pudo_types.Offer`, `WAIMatch`, `dispatch.Action`, `DriverQueue.snapshot`, etc.). Source: `driver_queue._project_offers` constructs `Offer(offer_id=str(o['id']))` from the `offer_history` row's primary key.

- **`decision_log.id`** (`integer`) — what the schema's foreign-key relationships expect. `pickup_market_signals.offer_id REFERENCES decision_log(id)`, and `pudo_decision_context` records `current_offer_id` historically as a `decision_log.id` value (per `get_next_stacked_offer`'s JOIN in `nail_it_core.py`).

These IDs diverge. The recon on 2026-05-08 found:

- **874 offer_history rows have `id != decision_log_id`** (out of 2251 total, with 1377 having NULL `decision_log_id`).
- **The divergence has grown to ~624 rows** between the two sequences as of 2026-05-08.
- **Recent rows alternate the divergence cleanly** — `id=7771 / decision_log_id=8395`, `id=7770 / decision_log_id=8394`, etc.

## How `_execute_action` was broken

Every FirePickup / FireDropoff write path in `_execute_action` (six SQL statements total) was authored assuming `action.offer_id == decision_log.id`:

```sql
UPDATE app_private.pickup_market_signals SET ... WHERE offer_id = %s::integer
UPDATE app_private.offer_history          SET ... WHERE decision_log_id = %s::integer
INSERT INTO public.community_offers ...    JOIN ... WHERE pms.offer_id = %s::integer
```

But `action.offer_id` is `offer_history.id` per the `Offer` constructor in `driver_queue._project_offers`. So every "Nail It" since the divergence began has been:

- Updating `pickup_market_signals` rows belonging to *different decisions* (or no rows at all when no decision_log row matched the offer_history.id value).
- Updating `offer_history` rows *that referenced an unrelated decision_log* (silently corrupting actual_pickup_lat/lng on the wrong offer record).
- Inserting `community_offers` rows with mis-attributed pickup coordinates (or, more often, no-op'ing because the JOIN found no match).

Symptom: production traffic showed `actual_pickup_at IS NULL` on virtually every recent accepted offer, despite FirePickup actions firing. The auto-nailer was technically functional but writing into the void.

## Why we didn't fix the root cause today

Option β (realign `Offer.offer_id` to `decision_log.id`) is the architecturally correct fix. It honors the schema's FK design and makes `_execute_action` correct without code changes. We deferred it because:

1. **Mid-sprint timing.** Item 3b.W (writer) and Item 3b.R (heartbeat reader) had just shipped, both built on the assumption that `Offer.offer_id == offer_history.id`. Cutting over mid-sprint risks shipping a half-realigned system.

2. **The 1,377 NULL `decision_log_id` rows.** Switching `Offer.offer_id` to `decision_log_id` makes those 1,377 rows non-addressable through the new contract. Until we know whether they're pre-history artifacts or active edge-case writes, we cannot tie the canonical contract to a nullable column.

3. **Cross-cutting blast radius.** Option β touches `pudo_types.Offer`, every `Offer(...)` constructor and `FakeOffer(...)` test fixture, every consumer of `WAIMatch.offer_id`, every test mock, and the just-shipped Item 3b.W and 3b.R SQL. That's a dedicated sprint of audit work.

4. **`Offer.offer_id: str` typing.** Decision_log.id is `integer` (32-bit); offer_history.id is `bigint`. Long-term scaling implications favor bigint, which would mean Option β should ALSO migrate `decision_log.id` to bigint at cutover time — additional schema migration risk.

## What the alpha-patch fixes

`_execute_action` now translates at the boundary:

```sql
-- Before (broken):
WHERE offer_id = %s::integer
-- After (alpha-patch):
WHERE offer_id = (SELECT decision_log_id FROM app_private.offer_history WHERE id = %s::bigint)
```

The translation lives at one place (`_execute_action`) and only one place. `Offer.offer_id`'s contract stays unchanged — it is and remains `offer_history.id`. The schema FKs stay unchanged. The bug stops bleeding.

The alpha-patch also adds a rowcount guard on the canonical `offer_history` UPDATE in both FirePickup and FireDropoff branches: if the UPDATE writes zero rows, log a WARNING and return `(False, "fire_*_zero_rows")` so `dispatch_executed=False` reflects reality. The pms UPDATE does NOT get a guard — pms is sparse (91% orphan rate as of 2026-05-08) and zero-row UPDATEs there are routine.

## Open questions for the future Option β sprint

When a future session takes up the realignment, the prerequisite recon should answer all of these before any code change:

1. **What populates the 1,377 NULL `decision_log_id` rows in offer_history?** Are they:
   - Pre-decision_log integration historical artifacts (safe to ignore)?
   - Test data from `/api/v1/test/seed_offer` flows (safe to scope-exclude)?
   - An active production code path that creates offer_history without a decision_log row (BLOCKER — Option β cannot proceed without resolving this)?

2. **What populates the 6,596 orphan `pickup_market_signals` rows?** (`pms.offer_id` values with no matching `offer_history.decision_log_id`.) Are these:
   - Declined offers (offer was logged in pms but never accepted into offer_history)?
   - Cross-driver writes from the broken `_execute_action` path (i.e., the bug we just fixed wrote to pms with the wrong ID)?
   - A separate code path that writes to pms without the corresponding offer_history population?

3. **Which test fixtures construct `Offer(offer_id=...)` or `FakeOffer(offer_id=...)`?** Full audit:
   ```
   grep -rn "Offer(offer_id=\|FakeOffer(offer_id=" tests/
   ```
   Each call site needs to update the value passed if `offer_id` semantics change.

4. **Is `WAIMatch.offer_id` propagated to any caller that uses it as a SQL identifier?** WAI generates `WAIMatch` from `Offer`; downstream consumers should treat it as opaque, but verify.

5. **Are `pudo_decision_context.current_offer_id` and `current_offer_id_at_eval` historical values currently `decision_log.id` strings, `offer_history.id` strings, or a mix?** If a mix, forensic queries need a backfill before the cutover or they'll mis-correlate.

6. **`driver_trip_state.current_offer_id`** — what value does the production heartbeat path actually write here? `DriverQueue.bind(action.offer_id)` passes `Offer.offer_id` (= `offer_history.id`), which means the trip-state pointer has been tracking offer_history.id values. But `get_next_stacked_offer` queries it as `dts.current_offer_id::integer = oh.decision_log_id`. So this path has been broken alongside `_execute_action`. Decide at Option β time whether to migrate trip-state values or fix the JOIN to use `oh.id`.

## Cleanup recommendations (not part of alpha-patch scope)

Once the data-archaeology questions above are resolved, a follow-up cleanup sprint should:

1. **Backfill or invalidate the 1,377 NULL `decision_log_id` rows in offer_history** depending on outcome of question 1.
2. **Backfill or DELETE the 6,596 orphan `pickup_market_signals` rows** depending on outcome of question 2.
3. **Diagnose and fix `get_next_stacked_offer`** in `nail_it_core.py` — its JOIN uses the broken `decision_log_id` semantic. May or may not need change depending on which ID we standardize on.
4. **Audit `pickup_market_signals.offer_id` FK** — currently REFERENCES decision_log(id), but actual usage suggests the intent may be different. The schema FK may need to change OR usage needs to align with FK.

## Severity / urgency

- **Pre-alpha-patch**: HIGH (production silently corrupting data on every Nail It).
- **Post-alpha-patch**: LOW-MEDIUM (no active bleeding; debt is the cognitive overhead of two ID systems coexisting and the future cleanup cost).
- **Trigger to re-prioritize**: any future sprint that needs to expand `Offer` semantics, add new offer-keyed tables, or unify the forensic chain across pudo_decision_context / pickup_market_signals / offer_history. At that point Option β becomes the dominant cost driver and should be done first.

## Recon evidence (preserved here so future sessions don't re-derive)

```
=== id vs decision_log_id alignment in recent offer_history (2026-05-08) ===
  id  | decision_log_id | diff
------+-----------------+------
 7771 |            8395 | -624
 7770 |            8394 | -624
 ... (all 20 most-recent rows show diff=-624) ...

=== correlation across the whole table ===
 total | aligned | divergent | null_dl
-------+---------+-----------+---------
  2251 |       0 |       874 |    1377

=== pms FK semantics (recent rows) ===
 pms_offer_id | dl_id | oh_dl_id  ← all three columns match
        8395  |  8395 |     8395
        ... (10/10 recent rows aligned) ...

=== pms orphan rate ===
 pms_total | pms_with_oh_match
-----------+-------------------
      7268 |               672    ← 91% orphan rate
```

---

End of tech-debt note.
