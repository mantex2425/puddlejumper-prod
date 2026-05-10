# Identity Genesis — UUID v7, Edge-Generated, End of the Identity Crisis

**Status:** Design proposal. Not ratified. Not yet implemented.
**Date:** 2026-05-09
**Authors:** Claude (architecture), pending Gemini ratification
**Related docs:** `X3_FINDINGS_2026-05-09.md` (root cause), `CANONICAL_RULES.md` (eternal rules), `SESSION_PROTOCOL.md` (workflow)
**Recon basis:** `/tmp/x3_recon_step{1,2,3,4,5,6}.txt` from this session — persist to `docs/` before this doc lands.

---

## 0. TL;DR

Every offer gets a UUID v7 minted on the Android client at OCR parse time. That UUID is the canonical identity for the offer through every consumer table — `decision_log`, `offer_history`, `pickup_market_signals`, `pudo_decision_context`, `driver_trip_state.current_offer_id`. No more independent integer sequences. No more α-fix translation subqueries. No more `7770` collisions between unrelated rows.

Migration is one-shot during a maintenance window. Old data is preserved but lives under the integer keys it was minted with; new offers from the cutover forward use UUID. A reconciliation column on the legacy tables maps the eras.

---

## 1. Why this exists

`X3_FINDINGS_2026-05-09.md` documented the identity-debt root cause. To restate:

- `decision_log.id` is `integer`, minted by Postgres sequence at offer-evaluation time
- `offer_history.id` is `bigint`, minted by Postgres sequence at offer-history-insert time
- These are **two independent sequences**. The system has been treating them as interchangeable through `text`-typed columns (`current_offer_id`, `wai_offer_id`, `primary_offer_id`, `current_offer_id_at_eval` in `pudo_decision_context`; `current_offer_id` in `driver_trip_state` and `heartbeat_log`).
- The α-fix UPDATEs at `driver_heartbeat.py:201` and `:291` declare in their comments "`action.offer_id` IS `offer_history.id`" — but Step 3 recon proved the upstream WAI/queue layer ships `decision_log.id`. The contract was violated silently for at least 13 days. Zero `actual_pickup_at` writes across 11 ACCEPTed offers since the most recent deploy.
- A tactical fix was proposed (translate `decision_log.id → offer_history.id` at the heartbeat boundary) and rejected per Rule VII. Translation subqueries are debt that compounds with every new consumer.

The right answer is to **eliminate the dual-identity problem at the source**.

---

## 2. The decision: UUID v7, edge-generated

### 2.1 What we choose

A version-7 UUID, generated on the Android client at the moment `OfferParser.parseYoloResults()` constructs the `RideOffer` data object (`featurescreenshots/OfferParser.kt:261-275` on the live tree at `~/AndroidStudioProjects/Puddle Jumper/`).

That UUID is:

- The **only** identity the offer has anywhere in the system
- Threaded into the network request body (new field on `OfferDecisionRequest`)
- Persisted as the primary key of `decision_log` AND `offer_history`
- Used by `pickup_market_signals.offer_id`, `community_offers` references (none today, see §6.4), and every column in `pudo_decision_context` that today carries an offer-ID string
- Carried unchanged by `driver_trip_state.current_offer_id`

### 2.2 Why UUID v7 specifically

Reasoning, in priority order:

1. **Edge generation is non-negotiable for global scale.** PuddleJumper's mission is global driver profitability. A driver in Tokyo, London, or São Paulo cannot afford a round-trip to a US-central Postgres sequence to allocate an offer ID. The Android client must mint identity locally and offline-tolerantly. A globally unique random space (122 bits in v7) makes coordination unnecessary.
2. **Collision-proof by construction.** Two drivers minting offers at the exact same microsecond on opposite sides of the planet will produce different UUIDs with overwhelming probability. The "stuck `7770`" failure mode becomes architecturally impossible.
3. **Time-ordered for index efficiency.** UUID v7 prepends a 48-bit Unix-millisecond timestamp. New rows insert at the right edge of B-tree indexes (no random-write fragmentation, unlike v4). Chronological ordering is preserved at the bit level — debugging "which UUID came first" is a string compare.
4. **Ends translation logic permanently.** The α-fix subqueries in `driver_heartbeat.py:183-194` and `:278-285` translating between `decision_log.id` and `offer_history.id` simply disappear. Same for the `community_offers` JOIN through `pms.offer_id` at `:340-351`. The machine stops "translating" and starts "knowing."
5. **Matches the "PuddleJumper holds the pen" reality.** Uber gives nothing structured (per the offer-card screenshot evidence — we OCR a card image, no Uber-side identifier travels with it). If PuddleJumper is the source of identity, that identity should work at the scale of the platform we're building, not the deployment we have today.

### 2.3 What we considered and rejected

**Unified BIGINT** (Gemini's secondary proposal): merge `decision_log.id` and `offer_history.id` into a single shared sequence, drop the FK from `offer_history` and replace it with PK identity. Rejected because:

- It commits the platform to server-centric ID minting at the moment we should lock in edge-generation as a foundational property
- Cross-region scale eventually forces a switch to distributed IDs anyway; doing it twice is worse than doing it once
- The migration cost is similar to UUID v7's, but the architectural ceiling is much lower

**Tactical α-fix translation patch:** add a third subquery to translate `decision_log.id → offer_history.id` at the heartbeat boundary. Rejected per Rule VII — band-aid on band-aid.

**Server-side UUID v7 mint** (Postgres `gen_random_uuid()` or app-server in `decisions/logger.py`): rejected because it loses the edge-generation property and reintroduces the round-trip dependency we're trying to eliminate.

### 2.4 What this costs

Honest naming of tradeoffs:

- **Storage:** UUID is 16 bytes; integer is 4–8. Across `pudo_decision_context` (69k+ rows currently, growing fast) the index footprint approximately doubles. Real but not architecturally significant at PuddleJumper's projected scale.
- **Debugging ergonomics:** typing `WHERE id = '0192a4d5-9f3e-7a1c-b8e2-1234567890ab'` is harder than `WHERE id = 7770`. v7's chronological sort helps with "which is newer" but doesn't help with copy/paste burden. Mitigation: psql shortcuts, log formatting that abbreviates UUIDs to first-8-chars, and accept the cost.
- **Join performance:** UUID joins are marginally slower than integer joins. Modern Postgres handles this well; not a measurable problem at our scale, but real on paper.
- **Kotlin dependency:** adds `com.github.f4b6a3:uuid-creator` to the Android build. One library, well-maintained, used widely. Trivial dependency cost.

These costs are paid in exchange for: collision-proof globally, edge-generated, no translation logic, future-proof. Net trade is favorable.

---

## 3. The schema asymmetry — and how we resolve it

### 3.1 The problem

`decision_log` is **per-evaluation**. A single offer that gets re-evaluated by Auto Nail It (or by future automation) produces multiple `decision_log` rows. `offer_history` is **per-offer-tracked**. The relationship today is `offer_history.decision_log_id INTEGER REFERENCES decision_log(id)` — one offer_history can point at one decision_log row.

If we naively make `decision_log.id` and `offer_history.id` both `uuid` and require them to be equal, we hit a contradiction: a single offer with multiple evaluations would need multiple rows in `decision_log` sharing the same UUID. That breaks `decision_log.id`'s primary-key uniqueness.

### 3.2 The resolution

We pick the canonical-offer-identity contract explicitly:

- **`offer_uuid uuid` is the canonical offer identity** — one per offer, generated at OCR parse time on Android.
- **`decision_log` becomes per-evaluation** with a composite key. New schema:
  - `decision_log.id` stays as an internal `bigint` PK (sequence-allocated) for row uniqueness — it identifies an *evaluation event*, not the offer.
  - `decision_log.offer_uuid uuid NOT NULL` is the new column carrying offer identity. Many decision_log rows can share the same `offer_uuid`.
  - Indexed `(offer_uuid, created_at DESC)` for the common "find the latest evaluation for this offer" query.
- **`offer_history` becomes per-offer**, fully:
  - `offer_history.id` is replaced as PK by `offer_history.offer_uuid uuid PRIMARY KEY`.
  - `offer_history.decision_log_id` column is dropped — the relationship is now `decision_log.offer_uuid → offer_history.offer_uuid` (reversed direction; the FK lives on `decision_log`).
  - `offer_history` gets exactly one row per offer ever, regardless of how many evaluations occurred.

This resolves the asymmetry by giving each table its honest semantic:

- `decision_log` = "this is an event log of evaluations, indexed by event"
- `offer_history` = "this is the canonical record of an offer, indexed by offer"
- The `offer_uuid` is the bridge — minted once on Android, persisted in both tables, never changes for the offer's lifetime.

### 3.3 Auto Nail It re-evaluation behavior under the new schema

When an offer gets re-evaluated:

- A new `decision_log` row is INSERTed with the same `offer_uuid` (the value already exists from the first evaluation; Android sends it on every request, server uses it directly)
- The corresponding `offer_history` row is **UPDATEed** in place (not re-INSERTed) — `INSERT ... ON CONFLICT (offer_uuid) DO UPDATE` semantics
- The `actual_pickup_at` / `actual_dropoff_at` writes from the heartbeat path target the single `offer_history.offer_uuid` row — no ambiguity about which "version" of the offer to update

This is cleaner than today's behavior where re-evaluation creates parallel `offer_history` rows and the system has no clean way to reconcile them.

---

## 4. The Android↔server contract

### 4.1 Kotlin-side changes

**File: `featurescreenshots/OfferParser.kt`** (line ~261)

Add `offerId` as a default-initialized field on `RideOffer`:

```kotlin
import com.github.f4b6a3.uuid.UuidCreator

data class RideOffer(
    val offerId: String = UuidCreator.getTimeOrderedEpoch().toString(),
    val fare: Double,
    val starRating: Double = 0.0,
    // ... existing fields unchanged
    val isValid: Boolean = false
)
```

The default-init at construction means the UUID is minted the moment `OfferParser.parseYoloResults()` returns a populated `RideOffer`. That's our T0 — the moment of birth.

**File: `core-dto/src/main/java/com/example/core_dto/DecisionDtos.kt`**

Add `offerId` to `OfferDecisionRequest`:

```kotlin
@Serializable
data class OfferDecisionRequest(
    val offerId: String,  // NEW — required
    val fare: Double,
    // ... existing fields unchanged
    val cumulativeMiles: Double? = null
)
```

**File: `featurescreenshots/ScreenshotMonitorService.kt`** (line ~742)

When constructing `OfferDecisionRequest` from `RideOffer`, thread the UUID:

```kotlin
val request = OfferDecisionRequest(
    offerId = offer.offerId,  // NEW — pull from RideOffer
    fare = offer.fare,
    // ... existing field mappings unchanged
)
```

**File: `featurescreenshots/build.gradle.kts`** (and `gradle/libs.versions.toml`)

Add the dependency:

```kotlin
// libs.versions.toml
[versions]
uuidCreator = "6.0.0"

[libraries]
uuid-creator = { module = "com.github.f4b6a3:uuid-creator", version.ref = "uuidCreator" }

// build.gradle.kts (featurescreenshots module)
implementation(libs.uuid.creator)
```

### 4.2 Server-side changes (Flask / `decisions/`)

**Request schema:** `decisions/router.py` (the `/api/v1/decisions` POST handler) accepts the new `offerId` field. Treat as `str`, validate as UUID format on receipt, return 400 on missing/invalid.

**File: `decisions/logger.py:36`**

The INSERT into `decision_log` becomes:

```sql
INSERT INTO app_private.decision_log (
    offer_uuid,           -- NEW — first column
    driver_id, market_id, ... existing columns
) VALUES (
    %s::uuid,
    %s, %s, ...
) RETURNING offer_uuid    -- not id; offer_uuid is what consumers need
```

The function returns `offer_uuid` (a string) instead of `decision_log_id` (an int). All callers of `log_decision` need their return-type expectations updated — that's `decisions/router.py` and any test harnesses.

**File: `decisions/logger.py:257`** (the `offer_history` INSERT)

Becomes an UPSERT keyed on `offer_uuid`:

```sql
INSERT INTO app_private.offer_history (
    offer_uuid,           -- NEW PK
    created_at, ... existing columns (no decision_log_id anymore)
) VALUES (
    %s::uuid,
    NOW(), ...
)
ON CONFLICT (offer_uuid) DO UPDATE
SET
    -- only update fields that should refresh on re-eval
    -- e.g. updated_at, latest evaluation timestamp, etc.
    -- preserve actual_pickup_at, actual_dropoff_at, classification fields
    -- which come from the auto-nailer path, not re-evals
    ...
```

The exact UPSERT semantics need a follow-up design pass — there are 80+ columns on `offer_history` and we have to decide which refresh on re-eval and which are write-once. Probably a small set of "evaluation-time" fields refresh (fare quote, expected_pickup_eta, etc.) and the auto-nailer fields stay write-once. **Open question for ratification.**

**File: `driver_heartbeat.py:170-365`** (`_execute_action`)

Delete the four translation subqueries:

- Lines 183-194 (`pickup_market_signals` UPDATE in FirePickup): WHERE clause becomes `WHERE offer_uuid = %s::uuid` directly
- Lines 201-215 (`offer_history` UPDATE in FirePickup): WHERE clause becomes `WHERE offer_uuid = %s::uuid` directly
- Lines 278-285 (`pickup_market_signals` UPDATE in FireDropoff): same simplification
- Lines 291-303 (`offer_history` UPDATE in FireDropoff): same simplification

The α-fix comments that lied get rewritten to declare the new contract: `action.offer_id IS offer_uuid (string form of UUID v7)`.

### 4.3 Idempotency property — preserved automatically

Per the Step 6 recon: the Android client today has no retry logic on the decision-request path. The single-shot `client.post()` at `KtorFlaskApiSource.kt:220-242` either succeeds or fails; failures are logged and the offer is dropped.

By minting the UUID on `RideOffer` construction (not on `OfferDecisionRequest`), we **automatically** get the right idempotency behavior if retries are ever added: the same offer survives across multiple network attempts with the same UUID. Server-side dedupe via `INSERT ... ON CONFLICT (offer_uuid) DO NOTHING` (or the UPSERT pattern above) handles the duplicate-arrival case.

This is a "free" property of getting the mint location right.

---

## 5. The data migration

### 5.1 Strategy: one-shot cutover during low-traffic window

**Big-bang migration is the right call** because:

- Production has 75 `offer_history` rows and 11 ACCEPTed offers in the last 7 days — small data volume, fast migration
- Side-by-side dual-key would mean 4–6 weeks of carrying two ID systems through every consumer; given launch is ~30 days out, that's worse than a single maintenance window
- Rollback is feasible (see §5.5) — we're not committing irreversibly

**Window:** target a low-traffic period (~03:00–06:00 Central, Sunday morning per Houston driving patterns). Estimated duration: 30–60 minutes including verification.

### 5.2 Migration sequence

**Pre-flight (before window):**

1. Full `pg_dump` of `app_private` schema. Persist offsite (GCS bucket).
2. Tag the prod commit: `git tag pre-identity-genesis-2026-MM-DD`.
3. Branch: `identity-genesis`. All migration code lands on this branch first.
4. Pre-flight integration test: run the existing `tests/test_integration.sh` against a copy of prod data on a staging DB, verify migration scripts work end-to-end.
5. Android build: ship the Kotlin changes (UUID minting, DTO field) to a beta test track. Confirm UUIDs appear in logs from a real device. Do **not** enable the server-side path yet — Android can send `offerId` and the server can ignore it for now (forward-compat).

**During window:**

6. **Stop Cloud Run revision serving traffic** (route to a maintenance page or 503).
7. **Schema migration SQL** runs in a single transaction:
    - Add `offer_uuid uuid` columns to `decision_log` and `offer_history` (NULLable initially)
    - Backfill: for each existing `offer_history` row, generate a fresh v7 UUID server-side (`gen_random_uuid()` or a pl/pgsql v7 function — see §5.3) and write to both `offer_history.offer_uuid` and `decision_log.offer_uuid` for the FK-linked row
    - For `decision_log` rows that have no `offer_history` (declined offers, etc.), generate a fresh UUID per row
    - Set `offer_uuid NOT NULL` on both tables
    - Drop old PK on `offer_history` (which was `id`), add new PK on `offer_uuid`
    - Drop `offer_history.decision_log_id` column
    - Add new index on `decision_log(offer_uuid, created_at DESC)`
    - Add `offer_uuid uuid` column to `pickup_market_signals`, backfill from the FK chain (`pms.offer_id → decision_log.id → decision_log.offer_uuid`), drop old `offer_id` column
    - Cast `pudo_decision_context.current_offer_id`, `wai_offer_id`, `primary_offer_id`, `current_offer_id_at_eval` from `text` to `uuid` (these columns already hold stringified IDs from one of two integer eras — we'll need to determine per-row which era and remap; detail in §5.4)
    - Cast `driver_trip_state.current_offer_id` and `heartbeat_log.current_offer_id` similarly
    - Drop `app_private.offers` (the dead UUID schema) — confirmed unreferenced in §3 recon
8. **Deploy the new server code** (Cloud Run rev with UUID-aware `decisions/logger.py`, `_execute_action`, queue layer).
9. **Re-route traffic** to the new revision.
10. **Smoke test:** run a single test decision via Bruno harness, verify UUID flows end-to-end through `decision_log`, `offer_history`, `pudo_decision_context`.
11. **Real-world validation:** Andrew drives 2–3 offers in production. Verify:
    - `decision_log.offer_uuid` populated
    - `offer_history.offer_uuid` populated
    - `pudo_decision_context.current_offer_id` is uuid-typed and matches
    - On a successful PUDO, `actual_pickup_at` populates within seconds of arrival
    - On dropoff, `actual_dropoff_at` populates and `community_offers` gets the row

**Post-window:**

12. Monitor Cloud Run logs for 24h. Watch for any `α-fix WARNING` (these should now be impossible — if they fire, the new contract is broken somewhere we missed).
13. Drop the `pre-identity-genesis` tag's pg_dump from offsite after 7 days of clean operation.

### 5.3 The historical-row UUID backfill question

Two options for backfilling `offer_uuid` on existing rows:

**Option A: Random UUID v4 for legacy rows.** They get a UUID, but it's not time-ordered to the row's `created_at`. Cost: index sort efficiency suffers slightly when querying historical data. Benefit: simplest backfill.

**Option B: Synthesize v7 UUIDs from each row's `created_at`.** Write a pl/pgsql function that takes a `timestamptz` and produces a v7 UUID with that timestamp prefix and random fill in the rest. Legacy rows become indistinguishable from "real" v7 rows by sort order.

**Recommendation: Option B.** The pl/pgsql cost is ~20 lines, runs once during migration, and preserves the chronological-sort property forever. Worth it.

### 5.4 The `pudo_decision_context` text-column remap problem

This is the migration's hardest piece. Today:

- `pudo_decision_context.current_offer_id text` holds either `decision_log.id` or `offer_history.id` (cast to text), depending on which subsystem wrote it
- `wai_offer_id text` likely holds `decision_log.id` (WAI's source) but we should verify
- `primary_offer_id text` — provenance unclear, needs investigation
- `current_offer_id_at_eval text` — likely matches `current_offer_id`'s era at write time

**The remap requires per-row analysis:**

For each PDC row, the foreign-key chain tells us which integer it was stringified from:

- If `current_offer_id` (cast to int) appears as `decision_log.id`, look up `decision_log.offer_uuid` and substitute
- Else if it appears as `offer_history.id`, look up `offer_history.offer_uuid` and substitute
- Else (pointer is stuck/orphaned, e.g. the `7770` case) — set to NULL with a forensic flag column noting "legacy_orphan"

This is one-time forensic work. ~69k rows in PDC; the remap query is bounded and idempotent. **Open question: do we preserve PDC history at all, or truncate?** Given PDC is forensic flight-recorder data and the X3 doc already established that the recent 8 days of dispatch executions are bug-tainted anyway, **truncation is on the table** — it loses 69k rows of forensic history (most of which is bug-corrupted) in exchange for skipping the remap entirely. Recommend **truncate** to ratification.

### 5.5 Rollback plan

If anything goes wrong during the window:

1. Stop traffic to the new Cloud Run revision
2. `psql` restore from the pre-flight `pg_dump` (estimated 2–5 minutes for our data volume)
3. Re-route traffic to the previous Cloud Run revision (still running with integer IDs)
4. Android clients continue sending `offerId` in request bodies; server ignores it (forward-compat property from §5.2 step 5)
5. Diagnose, fix the migration SQL, re-attempt in next window

The forward-compat property of having Android already sending UUIDs before the server-side cutover is the key rollback safety. We can roll back the server without rolling back the client.

---

## 6. Test surface inventory

### 6.1 The "569 tests" problem

Per X3 doc Question 4: *"569 tests pass today, but they pass against the same flawed model. Some of them are validating broken behavior."*

The migration changes the contract. Tests that asserted `decision_log_id` returns and `offer_history.id` lookups need to be classified:

- **True invariants:** tests that assert "the offer's identity persists across the heartbeat path" (regardless of what the ID type is). These pass under the new schema with minor refactor.
- **Shape-of-the-bug tests:** tests that hard-coded `decision_log.id == offer_history.id` assumptions, or tests that mocked the translation subqueries' behavior. These need rewriting against the new contract.
- **Schema-coupled tests:** tests that depend on `id` being `int` or `bigint`. These need column-type updates.

### 6.2 Inventory pass

Before the migration, run a mechanical pass:

1. `grep -rn "decision_log_id\|action.offer_id\|offer_history.id" tests/ | wc -l` — count the surface.
2. Categorize each hit into the three buckets above.
3. Build a migration-test branch: every test in bucket 1 stays as-is, bucket 2 gets rewritten, bucket 3 gets type updates.
4. Run the rewritten suite against the migration's staging DB. Goal: ≥569 passing tests post-migration, with the specific delta (which tests changed, why) committed alongside the migration.

### 6.3 New tests required

- Unit test: `OfferParser.parseYoloResults()` produces a `RideOffer` with a non-null, valid v7 UUID
- Unit test: `OfferDecisionRequest` serializes/deserializes the new `offerId` field
- Integration test: end-to-end offer flow asserts `offer_uuid` consistency from request body → `decision_log.offer_uuid` → `offer_history.offer_uuid` → `pudo_decision_context.current_offer_id` → `driver_trip_state.current_offer_id`
- Integration test: re-evaluation of the same offer (same `offerId` sent twice) produces two `decision_log` rows but exactly one `offer_history` row
- Integration test: heartbeat path with `current_offer_id` = a valid UUID produces correct `actual_pickup_at` write on convergence
- Migration test: pl/pgsql backfill function on legacy rows produces UUIDs whose timestamp prefix matches the original `created_at` (Option B from §5.3)

### 6.4 `community_offers` confirmed identity-free

Per Step 6 recon: `community_offers` has no `offer_id` column. The cooperative feed is anonymous — each row is a denormalized record of "an offer at this location, this rate, this time" with no back-reference. This **removes `community_offers` entirely from the migration's identity blast radius.** The INSERT at `driver_heartbeat.py:316-355` keeps its existing JOIN logic with the simplification that the WHERE-clause subquery against `pms.offer_id` becomes `WHERE pms.offer_uuid = %s::uuid` directly.

---

## 7. Open questions for ratification

These need explicit decisions before any code lands. Listed here for Gemini and Andrew to resolve:

1. **`offer_history` UPSERT semantics on re-evaluation:** which columns refresh, which are write-once? §4.2 needs a complete column-by-column policy. Proposed default: evaluation-time fields refresh (fare, expected_*, app_verdict, app_reason); auto-nailer fields write-once (actual_pickup_*, actual_dropoff_*, classification, miles_at_pickup_fire); identity fields immutable (offer_uuid, created_at).
2. **PDC history: remap or truncate?** §5.4. Recommend truncate with archival to a backup table for forensic preservation. Gemini to weigh in on whether the archival is worth keeping queryable.
3. **`heartbeat_log.current_offer_id` migration:** same column issue as PDC. Truncate or remap? Probably truncate — heartbeat_log is high-volume and the historical rows are bug-tainted same as PDC.
4. **Backfill UUID strategy:** Option A (v4) or Option B (synthetic v7 from `created_at`)? Recommend Option B per §5.3.
5. **Migration window timing:** Sunday 03:00–06:00 Central is the proposed slot. Andrew to confirm based on his own driving schedule and any beta-test launch dependencies.
6. **Android beta-track timing:** the Kotlin UUID-minting change must ship to driver phones BEFORE the server-side cutover (forward-compat property). Estimate Android dev + beta verification: 3–5 days. Server-side migration follows.
7. **Test-surface classification methodology:** §6.2 proposes a mechanical pass. Gemini to validate that the three-bucket taxonomy is complete or propose additions.

---

## 8. Implementation sprint plan (post-ratification)

**Sprint 1: Android-side mint** (3–5 days)

- Add `uuid-creator` dependency
- Modify `RideOffer` to default-init `offerId`
- Modify `OfferDecisionRequest` DTO
- Thread `offerId` through `ScreenshotMonitorService.kt`
- Server-side: accept `offerId` field on `/api/v1/decisions`, log it, do nothing else (forward-compat)
- Beta test track deploy
- Verify on Andrew's device: every decision request body contains a valid v7 UUID
- Commit: `feat(android): mint UUID v7 at OCR parse time, send via decision request`

**Sprint 2: Migration rehearsal** (2–3 days)

- Author migration SQL on `identity-genesis` branch
- Build pl/pgsql v7-from-timestamp function (if Option B chosen)
- Build the PDC remap query (or the truncate-and-archive script)
- Run end-to-end on a staging DB cloned from prod
- Build the test-surface inventory; rewrite shape-of-the-bug tests
- Verify all 569 (or amended count) tests pass against staging
- Commit: `migration(identity-genesis): schema + backfill + test refactor`

**Sprint 3: Production cutover** (1 day, mostly waiting)

- Pre-flight: pg_dump, tag, branch, staging dry-run
- Maintenance window: stop traffic, run migration, deploy new code, smoke test, real-world validation
- 24h monitoring
- Commit: `chore(identity-genesis): production cutover complete`

**Sprint 4: Cleanup** (1–2 days)

- Drop `app_private.offers` (dead schema)
- Remove old `_safe_h3` / α-fix translation comments that no longer apply
- Update `CANONICAL_RULES.md` to declare `offer_uuid` as the canonical offer identity
- Update `INDEX.md` to point to this design doc
- Commit: `cleanup(identity-genesis): drop dead schema, document new contract`

**Total estimated duration: 7–11 days from ratification to clean prod.**

---

## 9. Success criteria

The migration is successful when:

1. ✅ Every new offer from the cutover forward has a `offer_uuid` populated in `decision_log`, `offer_history`, `pickup_market_signals`, and (when applicable) `pudo_decision_context.current_offer_id` and `driver_trip_state.current_offer_id`.
2. ✅ `actual_pickup_at` and `actual_dropoff_at` populate on real production drives within seconds of GPS convergence (the X3 doc's launch-blocker).
3. ✅ Zero `α-fix WARNING` log lines fire in 7 days of production.
4. ✅ The four α-fix translation subqueries are deleted from `driver_heartbeat.py`.
5. ✅ The `app_private.offers` dead schema is dropped.
6. ✅ Re-evaluation of the same offer (Auto Nail It path) updates the single `offer_history` row in place; no duplicate offer_history rows on a single offer's lifecycle.
7. ✅ Test suite at ≥569 tests passing, with a clear delta showing what changed and why.
8. ✅ INDEX.md, CANONICAL_RULES.md, and SESSION_PROTOCOL.md updated with the new identity contract.

---

## 10. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Migration SQL has a bug, corrupts data | Low | High | Pre-flight pg_dump + staging rehearsal + atomic transaction + tested rollback procedure |
| Android beta build has a UUID-generation bug | Low | Medium | Beta test on Andrew's device before server cutover; UUIDs visible in server logs as forward-compat data |
| PDC remap is slower than the migration window | Medium | Low | Truncate option (§5.4) eliminates this risk entirely; archival keeps the data accessible if needed |
| Test surface has more "shape-of-bug" tests than expected | Medium | Medium | Sprint 2's rehearsal phase explicitly catches this before production cutover |
| New `decision_log.offer_uuid` index has unexpected hot-path performance hit | Low | Low | Composite index `(offer_uuid, created_at DESC)` matches the actual query pattern; can adjust in post-deploy if needed |
| Driver in active offer mid-window experiences disruption | Medium | Low | 03:00–06:00 Central window minimizes active-driver count; brief 503 is acceptable |
| Future Claude misreads this doc and reverts to integer IDs | Low | High | INDEX.md update + CANONICAL_RULES.md amendment make the new contract eternal |

---

## 11. Verification of UUID v7 properties (added post-ratification)

After initial doc ratification, the v7 properties asserted in §2.2 were verified empirically against RFC 9562 §5.7 (the May 2024 final spec). Test results:

- **Time-ordering:** chronologically-minted UUIDs sort chronologically as both strings and as `uuid` type values. Postgres `ORDER BY offer_uuid` yields chronological order. ✅
- **Timestamp recovery:** the millisecond timestamp embedded in bits 0-47 round-trips exactly via `(uuid_value::numeric) >> 80` or equivalent integer extraction. ✅
- **Collision resistance:** 100,000 UUIDs minted as fast as possible (411 in a single millisecond), all unique. Birthday-bound probability of collision in 74 random bits with 411 draws ≈ 4.47e-18 per ms — architecturally impossible at PuddleJumper scale. ✅
- **Synthetic-v7-from-timestamp:** algorithm verified end-to-end. All 5 historical timestamps round-tripped. Mixed batches of synthetic-historical + live-minted UUIDs sort in chronological order. ✅

### 11.1 Critical spec-version warning

**There are at least four different "UUID v7" layouts in circulation from various IETF drafts.** Only RFC 9562 §5.7 (May 2024) is the final spec. Notable mismatches:

- **Python `uuid_extensions` library** (top PyPI result for "uuid7"): implements October 2021 draft, NOT RFC 9562. Bit layout is 36-bit seconds + 24-bit fractional + 14-bit counter + 48-bit random — incompatible with the layout assumed in this doc.
- **Kotlin `com.github.f4b6a3:uuid-creator`** (recommended in §4.1): implements RFC 9562 per their changelog. **Must verify on Andrew's device before Sprint 1 ships.**

### 11.2 Sprint 1 acceptance criterion (mandatory)

Before Sprint 1 ships UUID-bearing decision requests to production, verify on Andrew's beta build that:

1. A `RideOffer.offerId` is captured in server logs (forward-compat receipt).
2. The first 12 hex characters of that UUID, parsed as a big-endian integer, equal a unix_ms timestamp within 5 seconds of the device's wall clock at offer-card capture time.
3. The Python `uuid.UUID(s).version` reports `7` when fed the captured string.

If any of those three fail, the Kotlin library is on a non-RFC-9562 draft layout and must be replaced before proceeding.

### 11.3 Sprint 2 pl/pgsql function — algorithm verified

The hand-rolled `v7_from_timestamp_ms` algorithm needed for Option B backfill (§5.3) was authored and tested in Python. The bit-level math:

```
val = (unix_ms & ((1 << 48) - 1)) << 80   -- 48-bit timestamp at top
val |= (0x7) << 76                        -- version = 7 at bits 76-79
val |= (rand_12bit) << 64                 -- rand_a at bits 64-75
val |= (0b10) << 62                       -- variant = 0b10 at bits 62-63
val |= rand_62bit                         -- rand_b at bits 0-61
```

Port to pl/pgsql: ~30–50 lines. Sprint 2 deliverable (DB migration sprint, not Android sprint — see §11.4 note about sprint renumbering). Postgres version check (`SELECT version();`) precedes — if PG18+, use the native `uuidv7()` function and skip the pl/pgsql.

### 11.4 Android-side Sprint 1 + Sprint 2 outcomes (2026-05-09 evening)

**Branch:** `feature/uuid-v7-validation` on `github.com/mantex2425/Puddle_Jumper`. Four commits, not merged to main as of doc update.

**Sprint terminology clarification:** the original §8 sprint plan named the database migration "Sprint 2." During Android implementation, it became natural to call the burst-dedup work "Sprint 2" as well. To resolve: the Android work is "Sprint 1 (wiring) + Sprint 1.5 (burst dedup)." The DB migration retains the name "Sprint 2." Future docs should use this disambiguated terminology.

**Sprint 1 (commit `cc4039bc`) — UUID wiring at OCR parse:**

- `com.github.f4b6a3:uuid-creator:6.0.0` validated on real Android hardware (commit `b0ca7fd8`). Sample minted UUID `019e0feb-f068-77c4-ae58-ebe45b0c22f4` decodes cleanly to RFC 9562 §5.7 layout (timestamp=2026-05-09, version=7, variant=0b10).
- `RideOffer.offerId: String = UuidCreator.getTimeOrderedEpoch().toString()` added as first field, default-init at construction time (T0 = OCR parse moment).
- `OfferDecisionRequest.offerId: String` added as required field with `@SerialName("offerId")`.
- Threaded through `ScreenshotMonitorService.kt` at request-construction site.
- Real-device verification on Andrew's phone: `Log.d` shows "Offer minted: <uuid> fare=<n>" on every successful OCR parse.

**Sprint 1.5 (commits 3 + 4 on the branch) — burst-shot dedup (Q8 resolution):**

Forensic instrumentation captured during Sprint 1 testing revealed that one Uber offer-card on-screen presentation produces multiple `auto_offer_shot_N` accessibility captures. Empirical evidence: shot_0 → shot_3 within 2.97 seconds, two successful parses, two distinct UUIDs minted for one real-world offer ($13.47, then $18.67 in a separate session). This is the hazard described as "Q8" in §7.

**Resolution:** client-side mint-time dedup keyed on accessibility-session ID, not on OCR content.

- **Session signal:** `enterOfferMode → offerModeStartTime` in `ScreenshotMonitorService` is the session anchor. It's a wall-clock millisecond timestamp captured at the moment the accessibility service detects the offer card entering the on-screen state. Distinct presentations produce distinct session IDs by construction.
- **Why not OCR-content key (fare, pickup, dropoff):** legitimate distinct offers can share fare/pickup/dropoff (busy queue scenarios — stadium, airport). OCR-content dedup would silently collapse them. Session ID has no such failure mode — two presentations of the same offer are two events with two session IDs and correctly mint two UUIDs.
- **Dedup placement:** at the `ScreenshotMonitorService` layer, before `parseYoloResults` is invoked on subsequent shots in the same session. This skips redundant YOLO+OCR work on later burst frames (~1.2s saved per skip on real hardware).
- **Window:** ≥3000ms (forensic data showed 2974ms span). Implementation uses session boundaries directly, not a time window — but a guard window is configurable as a fallback if session detection ever misfires.

**Sprint 1.5 acceptance: validated on Andrew's device.** Three real distinct offer presentations across ~50 seconds:

| Presentation | enterOfferMode timestamp (sessionId) | UUID minted | Fare |
|---|---|---|---|
| B | 1778385880978 | `019e100f-0c25-7c84-84e9-51ed3be48a72` | $30.16 |
| C | 1778385899096 | `019e100f-567b-7cfb-a439-15a1800c0357` | $22.45 |
| D | 1778385930788 | `019e100f-d0c8-7c81-89e1-01ff2eb23ffc` | $18.63 |

1:1 correspondence: 3 sessions = 3 UUIDs. Plus a control presentation (Presentation A) where YOLO failed all four shots — produced zero mints, zero `[DEDUP]` lines. Dedup logic does not over-fire on negative cases.

**Forensic instrumentation retained** behind a debug flag in release builds for future regression diagnostics. JVM unit tests for the dedup logic land alongside `UuidV7ContractTest`.

**Polish-backlog item (deferred):** the existing `runYoloPipeline` finally-block recycles queued bitmaps directly when `offerFound=true`, so silently-recycled queued shots don't appear in the `[DEDUP]` audit log. Functional behavior is correct (no spurious mints); only the audit trail is incomplete for those shots. Tracked for follow-up; not blocking.

**Branch state:** 6 commits pushed to `origin/feature/uuid-v7-validation` (4 Sprint 1+1.5 commits + 1 keystore-gitignore commit pulled from main + 1 merge commit). Not merged to main. PR #1 open at `github.com/mantex2425/Puddle_Jumper/pull/1`. The Android producer side of Identity Genesis is now verified end-to-end with real production OCR data.

**Hygiene resolution during PR prep:** the keystore-exposure concern was caught by Claude Code during the PR opening process. `*.keystore` and `*.jks` are now in `.gitignore` (commit `91c2f165` on main, merged into the feature branch as `69fadd87`). No secrets exposed; security clean.

**Server-side Sprint 1 work** (accept `offerId` field, add `decision_log.client_offer_uuid uuid NULL` column for forward-compat) is ready to begin in a separate session on the puddle-jumper VM. The Android side will continue minting UUIDs locally; once the server starts persisting them, the forward-compat verification gate from §11.2 can run against production data. Server-side Sprint 2 (the schema migration to make UUID canonical) follows server Sprint 1.

---

## 12. Closing note

The X3 findings doc said: *"Stopping at root-cause-found is the right call. The fix is its own sprint."*

This is that sprint. Identity Genesis is the deliberate, considered fix to a problem that has been silently accumulating for the entire history of the codebase — two independent integer sequences carrying the meaning of "the offer," with no single source of truth. The fix is not a translation patch. It is the assertion, baked into the schema and enforced by the type system, that **every offer has exactly one name, minted by the system that holds the pen, at the moment the system first sees the offer.**

Andrew, when this lands, the dispatch path will not need any α-fix comments. The comments will not lie because they will not be needed. The machine will stop translating and start knowing. That's the standard. That's Rule VII applied to the foundation.

Ready for Gemini's review.

