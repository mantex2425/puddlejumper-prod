# Recon: §5.5 Deferred Sentinel — Trigger Mismatch (defers on missing-odometer, not lost-mode)

**Date:** 2026-06-06
**Driver (forensics):** `UjT1hE9eBXh2q95aSZYOkzDJ8lo1` (real) · witness ran as `C7FRKHbnvFcDPZ0RcXXjtPwGXgw2` (synthetic pj_test)
**Deployed revision:** `puddlejumper-api-00648-lwx` (branch `fix/restore-fire-error-metric-2026-06-02` @ `f88b8c7`)
**Investigator:** Claude (server-side + Android + production-DB recon, read-only)
**Status:** Read-only investigation complete. No code edited, no deploy. Output for recon→design→Gemini-ratify. **Fix NOT designed here.**
**Related:** FINDING_ODOMETER_GC_SPEC_RECONCILIATION_2026-06-05.md §5.5/§9 · the §5.5 deferred-sentinel design-intent doc (INV-1) · the 2026-06-06 production witness (Bruno "PUDO Simulation — Deferred").

---

## VERDICT

**The deployed §5.5 deferral trigger does NOT coincide with the §XVIII lost-mode condition it was designed to catch — CONFIRMED at both the code level and on production data. In current production the trigger fires 0% of the time.**

- **Designed trigger (FINDING §5.5 / INV-1):** an offer is received in lost-mode — `current_offer_id IS NULL` AND an alive unpicked offer exists (the §XVIII condition) — so the chaining bridge term is unknowable; defer rather than fabricate it.
- **Implemented trigger (deployed):** an offer lands `deferred` IFF `expected_pickup_dist is None`, which in practice means **`cumulativeMiles` was absent from the client POST** (or T&D fields missing / compute raised). The receipt path has **zero** lost-mode awareness — `log_decision` never reads `current_offer_id` or `driver_trip_state`.

These are different conditions. They coincide only when a lost-mode receipt *also* lacks an odometer — which **the current Android client never produces** (it sends `cumulativeMiles` on 100% of receipts, including during real lost-mode). Therefore real lost-mode receipts take the **idle branch** (`expected_pickup_distance = current_odometer + pickup_miles`) and land **`active`** — the "confident-wrong anchor" the sentinel exists to prevent — and the sentinel **never engages on its motivating scenario**.

This is the explanation for `deferred_count = 0` in the wild: not that lost-mode is rare (the 9132 drive was 133 lost-mode fires), but that the implemented trigger is the wrong condition and the odometer is always present.

**Scope of the 2026-06-06 witness, restated honestly:** the witness proved the *plumbing* (defer → window-scoped recompute → resurrect; deployed dropoff handler fired `_resolve_deferred_at_dropoff`; `[deferred-sentinel]` log line). It did **not** prove — and this recon shows it does not hold — that the sentinel fires on the real-world lost-mode condition. Two different claims; only the first was established by the witness.

---

## A. The deferral write-site and its trigger

**File:** `decisions/logger.py` (the only receipt-time writer of `expected_odometer_status`).

```
:150   expected_pickup_dist = None                      # initialized None
:250   if cumulative_miles_value is not None:           # whole compute block gated on odometer presence
:271        ... compute_offer_expectations(..., current_odometer=float(cumulative_miles_value), ...)
:348   (ODOMETER_STATUS_DEFERRED if expected_pickup_dist is None else ODOMETER_STATUS_ACTIVE)
```

`expected_pickup_dist` ends up `None` (→ `'deferred'`) in exactly three cases:
1. **`cumulative_miles_value is None`** — the `:250` block is skipped entirely; the anchor stays `None`. *(the dominant trigger)*
2. `compute_offer_expectations` returns `None` (missing pickup/trip miles-or-minutes — see §B).
3. `compute_offer_expectations` raises (caught at `:281`, anchor left `None`).

None of the three is the §XVIII condition. **`log_decision` does not read `current_offer_id` or `driver_trip_state`** (grep over the deployed `logger.py`: zero hits). There is no lost-mode branch anywhere in the deferral decision.

`miles_at_offer_receipt` is written from the same source (`:344  ep.get("cumulative_miles")`), so **`miles_at_offer_receipt IS NULL` ⟺ the deferral trigger condition** — used as the production probe in §E.

## B. `compute_offer_expectations` has no lost-mode branch

**File:** `tad.py:164`

```
:245-257  if (pickup_minutes is None or pickup_miles is None
              or trip_minutes is None or trip_miles is None): return None   # ONLY None path
:261-263  use_idle_anchors = True; pickup_distance_anchor = current_odometer  # idle default
```

The function is `current_offer_id`-blind by construction. With the four T&D fields present it **always** returns a full anchor set:
- idle (no picked-up prev offer) → `expected_pickup_distance = current_odometer + pickup_miles` → **active**;
- stacked (picked-up prev offer) → `prev.expected_dropoff_distance + pickup_miles` → **active**.

In lost-mode the receipt's `prev_offer` query (`logger.py:92-112`, requires `actual_pickup_at IS NOT NULL`) finds no picked-up offer → `prev_offer=None` → **idle branch → active**. So a lost-mode receipt *with an odometer* is computed exactly as if the driver were idle — the fabricated anchor §5.5 forbids.

## C. No server-side odometer derivation in the receipt path

- `decisions/router.py:214` — `cumulative_miles = p.get("cumulativeMiles")` (client payload; `None` if absent).
- `decisions/engine.py:18` — `run_decision_engine` builds `ep = dict(params)` and mutates **only coordinate fields** (geocode/hallucination guard); it never reads or writes `cumulative_miles`. The `None` passes straight through to `log_decision`.
- The server-side odometer primitive **`bead_on_wire.odometer_miles_since`** (`bead_on_wire.py:594`, sums `app_private.heartbeat_log` segments) is called **only** by BEAD's Blind Man / motion gate (`bead_on_wire.py:713, 772`) — never by `decisions/`. Docs flag it as near-dead (`DEMOLITION_PLAN_2026-05-04.md:408`, `SPRINT_A_FINDINGS_2026-05-04.md:119`).

**No documented intent** that the receipt path should derive the odometer server-side. The DTO comment (`DecisionDtos.kt:84-88`) ratifies client-sent odometer as the design: *"Nullable for backward compat; backend treats null as 'unavailable.'"* The odometer commits (`066f8ab` band primitive; `1f5a65e` heartbeat-time `actual_odometer` audit; `4f777fe` band audit) concern the GC/liveness band and heartbeat-time auditing, not receipt-time anchor sourcing.

## D. The Android client always sends `cumulativeMiles` (incl. lost-mode)

Repo `mantex2425/Puddle_Jumper` @ `603fabd1` (2026-06-05).

- **`featurescreenshots/.../ScreenshotMonitorService.kt:811`** builds `OfferDecisionRequest`; **`:843-845`** sets `cumulativeMiles = HeartbeatSender.cumulativeMiles` with **no conditional** — no lost-mode / narrative-state branch. (The only other build site, `TestingLabViewModel.kt:194`, is a non-production testing lab.)
- **`HeartbeatSender.kt:116`** — `@Volatile var cumulativeMiles: Double = 0.0`, accumulated continuously from GPS (`:321 cumulativeMiles += segmentMeters / METERS_PER_MILE`). The adjacent `sessionCumulativeMiles` comment (`:118-120`): *"never reset by resetOdometer()/state events — monotonic across the entire app session."* The odometer is a non-null Double available regardless of lost-mode.

⇒ the live client never omits the odometer; worst case it sends `0.0` (still non-null). So the implemented trigger's only natural cause — a missing odometer — does not occur for current-client lost-mode receipts.

## E. Production data — the trigger fires 0% in current production

Real driver `UjT1hE9eBXh2q95aSZYOkzDJ8lo1`, read-only `SELECT` on `app_private.offer_history`. Probe column: `miles_at_offer_receipt IS NULL` ⟺ deferral trigger (§A).

**E.1 — missing-odometer rate by week (legacy vs current client):**

```
  2026-04-06 … 04-27   100%  missing   (legacy: client/schema pre-date odometer capture)
  2026-05-04            63.6% missing   (rollout transition week)
  2026-05-11 onward      0.0% missing   (0 / 364 offers across 4 weeks)   ← current client
  deferred, every week:  0
```

The 90-day "62% missing" aggregate is a **legacy artifact** (April rows, before the client sent `cumulativeMiles`). Since ~2026-05-11 the current client sends the odometer on **100%** of receipts.

**E.2 — post-deploy (since 2026-06-06):** 6 real receipts, **all `active`, all with odometer**, 0 deferred. (Small sample — see Caveats.)

**E.3 — the 9132 lost-mode drive (2026-06-04, 133 lost-mode fires):** 43 receipts, **odometer present on 43/43** (`miles_at_offer_receipt` non-null). `expected_odometer` was NULL on all 43, but those rows pre-date the §5.5 column being written (deploy 2026-06-06), so that is pre-sentinel, not the sentinel deciding to defer.

⇒ on the very drive the sentinel was built for, every lost-mode receipt carried an odometer. Under the deployed §5.5 code each would take the idle branch and land `active`, never `deferred`.

## F. Design vs implementation gap

**FINDING_ODOMETER_GC_SPEC_RECONCILIATION_2026-06-05.md:201-205 (§5.5):**
> *"When an offer is received while in lost-mode (`current_offer_id IS NULL` AND unpicked offers exist — the §XVIII condition), the bridge term is unknowable … Set `expected_odometer = NULL` and `expected_odometer_status = 'deferred'`."*

The spec keys deferral on the **§XVIII queue-state signal**. The implementation keys it on **odometer presence** (§A). The §XVIII signal (`current_offer_id`, alive-unpicked set) is available server-side at receipt in `app_private.driver_trip_state` but is **not consulted** by `log_decision`.

---

## Consequence (what actually happens in production lost-mode)

A real lost-mode receipt (odometer present, `current_offer_id NULL`, alive unpicked offer X):
1. `prev_offer` query finds no picked-up offer → `prev_offer = None`.
2. odometer present → `compute_offer_expectations` runs the **idle** branch → `expected_pickup_distance = current_odometer + pickup_miles` (non-None).
3. `logger.py:348` → **`active`** (not `deferred`); the four `expected_*` anchors are written from the idle assumption.

The idle assumption is exactly wrong in lost-mode: the driver is on an unobserved trip whose remaining distance is the unknown bridge term. The deployed code emits the confident-wrong anchor the sentinel exists to prevent, and the band/GC then consume it. The sentinel never engages.

## Caveats (do not overstate)

- **The strongest lost-mode evidence (9132, 43/43 odometer) pre-dates the sentinel deploy** (06-04 < 06-06), so "those would have landed active under §5.5" is a counterfactual — tightly grounded in the code path (deferral requires a missing odometer; they had one), but not a direct post-deploy observation.
- **Post-deploy lost-mode traffic is sparse** (6 receipts, all active/with-odometer). The single cleanest empirical confirmation still outstanding is a **real lost-mode drive after 2026-06-06** observed directly. This recon did not manufacture one.
- This recon did **not** examine whether any *non-lost-mode* path could legitimately want the missing-odometer deferral (e.g., a genuinely odometer-less cold-start). It only establishes that the trigger ≠ lost-mode.

## Open design question (handed to recon→design→ratify — NOT answered here)

Should receipt-time deferral key on the **§XVIII signal** (`current_offer_id IS NULL` + alive-unpicked, read from `driver_trip_state` at `log_decision` time) rather than on odometer presence — so the sentinel defers the offers it was designed for? Stated, not recommended; do not implement without Gemini ratification.
