# Phase 2c.2 Item 3b + 3d Handoff — Caller-Layer Resume Brief

**For:** the next architecture-chat session continuing this sprint
**From:** the chat that just shipped Items 3a/3c/3e + tests (commits `07a003e`, `9cff775`)
**Date:** 2026-05-08
**Branch:** `phase-2c-2-tad-exit-4tools` off `f3f5dc9` on `demolition-2026-05-04`

---

## Read first (paste at session start)

Documents to load before any work:

1. **`docs/PHASE_2C_2_SPRINT_BIBLE.md`** — operative spec, especially Rules 1, 3a, 3b, 5, 7, 8 and the v2.1 Section III (UTC), V (forensic), VI (Auto Nail It only) anchors.
2. **`docs/SESSION_PROTOCOL.md`** — paired-programming workflow, paste-safety hazards (L-22 was confirmed real again this session), L-3 envelope discipline.
3. **`docs/PHASE_2C_2_ITEM_3_HANDOFF.md`** (commit `949fff4`) — original Item 3 handoff. Items 3a/3c/3e are now shipped; this doc covers the items it deferred.
4. **This document** — picks up where Item 3 handoff left off.

After loading docs, run a quick git recon:

```bash
cd ~/puddlejumper-prod && git log --oneline f3f5dc9..HEAD
```

You should see eleven commits ahead of base:

```
9cff775 phase 2c.2 brain tests: item 3 dual commit rule + classify_commit_rule (21 tests)
07a003e phase 2c.2 brain: item 3a + 3c + 3e (Step 4.5 TAD bouncer + dual commit rule + forensic blob)
949fff4 docs: phase 2c.2 item 3 handoff for next session
9c326fc phase 2c.2 brain: head 4 + patch 2c (poi witness signals fully wired)
1d84248 docs: phase 2c.2 sprint bible amendment for lost mode + inferred dropoff
a77c211 docs: phase 2c.2 sprint handoff for next session
de2ba5d phase 2c.2 brain: tad.py Part 2 + Lost Mode (evaluate_tad_gate)
61094d0 phase 2c.2 brain: tad.py Part 1 (compute_offer_expectations)
386ba3f phase 2c.2 prereq: extend Offer with pickup_minutes/trip_minutes
7a236a3 schema: phase 2c.2 migration applied to prod
5be275e docs: phase 2c.2 sprint bible (operative spec, supersedes kickoff)
```

Pytest baseline: **513/513 passed** (was 492 pre-Item-3, +21 from this session).

---

## What's landed since the last handoff

### Commit `07a003e`: Items 3a + 3c + 3e in `where_am_i.py`

Production code (`where_am_i.py`, +184/-12):
- `from dataclasses import dataclass, field` — `field` added for `default_factory=dict` use
- `from tad import evaluate_tad_gate, OfferTadState, TadVerdict` — new
- 3 module-level constants near top of file:
  - `COMMIT_NORMAL_HIGH = 0.90` (Bible Rule 3a, standalone)
  - `COMMIT_NORMAL_ELEVATOR = 0.80` (Bible Rule 3a, with `poi_type_match=True`)
  - `COMMIT_LOST_FLOOR = 0.85` (Bible Rule 3b, mandatory `poi_type_match=True`)
- `DiagnosticContext` extended: new field `tad_verdicts: dict = field(default_factory=dict)`. Defaulted so existing 4 test-fixture constructors (and the 2 production constructors in the no-cluster early-return path) work without modification.
- `evaluate()`, `evaluate_with_diagnostics()`, `_evaluate()` signatures gain 4 optional kwargs:
  - `per_offer_state: Optional[dict[str, OfferTadState]] = None`
  - `lost_mode: bool = False`
  - `last_known_anchor_id: Optional[str] = None`
  - `current_odometer: Optional[float] = None`
- **Step 4.5 TAD bouncer**: parallel pass over the queue producing `dict[offer_id, TadVerdict]`. Bypassed in bridge state (when `per_offer_state` or `current_odometer` is None) — empty dict means "no filtering" at Step 5 and "legacy floor" at Step 6.
- **Step 5 dispatch**: skip `_CLASS_DISPATCH[address_class](...)` invocation when `verdicts.get(offer_id).passed is False`. Per Bible Rule 1, this is the wallet gate — TAD-failed offers never trigger Google reverse-geocode spend.
- **Step 6 dual commit rule**: replaces `outcome.matched and outcome.confidence >= WAI_CONFIDENCE_THRESHOLD` with `_commits(outcome, tad_verdicts.get(offer_id))`. The `_commits` helper handles three branches: bridge state (legacy 0.40 floor), Normal Mode (Rule 3a elevator), Lost Mode (Rule 3b strict floor).
- **`_commits(outcome, verdict) -> bool`** new pure-function helper.
- **`classify_commit_rule(outcome, verdict) -> str`** new pure-function helper for forensic JSONB classification: returns `"normal_high"` / `"normal_elevator"` / `"lost_floor"` / `"legacy_floor"` / `"unknown"`.
- Both `DiagnosticContext(...)` construction sites updated to pass `tad_verdicts={}` (no-cluster early return) or `tad_verdicts=tad_verdicts` (final return).

### Commit `9cff775`: Item 3 tests in `tests/test_where_am_i.py`

3 new test classes, 21 new tests, +494/-0:
- `TestCommitsHelper` (7 tests) — pure-function tests for `_commits()` covering every rule branch including the True/False/None distinction for `poi_type_match`.
- `TestClassifyCommitRule` (6 tests) — pure-function tests for `classify_commit_rule()` including a table-driven cross-check that `_commits` and `classify_commit_rule` stay in sync.
- `TestItem3DualCommitRule` (8 tests) — integration tests against `_evaluate` with `evaluate_tad_gate` patched at module level and `_CLASS_DISPATCH` patched as a dict. Coverage of bridge-state preservation, Step 4.5 wiring, Step 5 skip-on-passed-False, Step 6 Normal Mode and Lost Mode commit branches, empty queue with `lost_mode=True`.
- New helpers: `_make_outcome()`, `_make_verdict()`, `_wai_with_fakes()` — mirror existing `_cluster()/_topo()/_target()/_offer()` factory style.

---

## The bridge state — what it means in practice

**Production behavior change since this session: none.**

Items 3a/3c/3e ship the new logic but make it inert. The new code path activates only when callers supply `per_offer_state` and `current_odometer` to `evaluate_with_diagnostics()`. Until Item 3b lands, every production caller (`driver_heartbeat.py:621`, `scripts/replay_pudo.py:434`, `tests/test_scenarios.py:221`) calls with the legacy 2-arg signature `(driver_id, queue)`, falling through to the bridge-state code path:

- Step 4.5 is skipped (the `if queue and per_offer_state is not None and current_odometer is not None:` guard fails)
- `tad_verdicts` is the empty dict `{}`
- Step 5 dispatch loop sees `if tad_verdicts:` evaluate False — no offers are skipped
- Step 6 calls `_commits(outcome, None)` for every candidate — `_commits` falls through to the legacy `conf >= WAI_CONFIDENCE_THRESHOLD` branch (0.40 floor)
- `DiagnosticContext.tad_verdicts` is `{}` — forensic blob serialization writes empty JSONB

**This is correct behavior, not a workaround.** It lets the production code land without lockstep coordination of the caller layer. Item 3b's job is to start supplying the TAD context, which automatically activates the new logic.

The 513 pytest cases prove bridge state preserves legacy behavior end-to-end including the 69 TOML scenario replays in `test_scenarios.py`. Any future change to Item 3 logic must keep bridge state intact (existing test suite is the regression net).

---

## Your scope (Items 3b + 3d)

Two work streams. Both interior to caller-layer code, not `where_am_i.py`.

### Item 3b: Lost Mode detection + TAD context assembly

**Where it lands:** `driver_heartbeat.py` (recon found `evaluate_with_diagnostics` invocation at line 621, in a function named approximately `_handle_post_pickup` or similar — verify on session open).

**What it does:** for each heartbeat that reaches `wai.evaluate_with_diagnostics(...)`:

1. **Assemble `dict[offer_id, OfferTadState]`** from `offer_history` rows alongside the queue. The 9-field `OfferTadState` shape is locked at `tad.py:316`:

   ```python
   @dataclass(frozen=True)
   class OfferTadState:
       offer_id: str
       miles_at_offer_receipt: float
       accepted_at: datetime
       expected_pickup_arrival_time: datetime
       expected_pickup_distance: float
       actual_pickup_at: Optional[datetime]
       cumulative_miles_at_pickup_fire: Optional[float]
       pickup_exit_time: Optional[datetime]
       exit_velocity_timeout: bool
   ```

   Caller fetches these columns from `app_private.offer_history` for every offer in the queue. Phase 1 schema migration already added the four `expected_*` columns plus the three exit-velocity columns (`pickup_exit_time`, `pickup_exit_odometer`, `exit_velocity_timeout`).

   **Note on the `pickup_exit_time` field**: this is populated by Phase 2 of the sprint (`escape_detection.py`, the post-pickup exit velocity detector). Phase 2 has NOT shipped yet — that's a separate Claude Code session per the Sprint Bible. For Item 3b's first cut, `pickup_exit_time` will always be NULL (column exists, no writer yet), and `evaluate_tad_gate` handles NULL cleanly (it forces dropoff time signal to applied=False per `tad.py` Section F). When Phase 2 lands, the field starts populating and dropoff time signals start activating without any change to Item 3b code.

2. **Detect Lost Mode (caller-driven, narrative_blindness).** Set `lost_mode=True` when:
   - Queue empty (first offer of session) — but in this case there are no offers to evaluate anyway; trivial.
   - Prior offer GC'd before pickup confirmation (queue contains a fresh offer but no prior anchor in `offer_history`).
   - Stacked offer received with no completed prior pickup-confirmation event.

   The narrative_violation case (per-offer odometer > 115% of expected) is detected inside `evaluate_tad_gate` itself — caller doesn't need to detect it.

3. **Track `last_known_anchor_id`** from session state — most recent offer_id where pickup OR dropoff was spatially confirmed. Used for forensic capture when Lost Mode fires.

4. **Pass `current_odometer`** from the latest heartbeat's `cumulative_miles` field.

5. **Forward all four into the call:**
   ```python
   matches, diagnostics = wai.evaluate_with_diagnostics(
       driver_id, snap.offers,
       per_offer_state=per_offer_state,
       lost_mode=lost_mode,
       last_known_anchor_id=last_known_anchor_id,
       current_odometer=heartbeat.cumulative_miles,
   )
   ```

6. **Serialize `diagnostics.tad_verdicts` to JSONB** at the `pudo_decision_context` write site. The dict is `{offer_id: TadVerdict}` and `TadVerdict` is already JSONB-shaped (its `distance_gate` and `time_signal` fields are dicts; `passed` is bool/None; `time_boost` is float; `leg_evaluated` and `lost_mode_reason` are str/None). One-shot `json.dumps` should serialize cleanly. The target column `pudo_decision_context.tad_decision_context jsonb` was added in the Phase 1 schema migration.

   For each committed match, also persist the `classify_commit_rule(outcome, verdict)` label — gives forensic queries a clean way to ask "how often did Head 4 lift a candidate?"

**Tests:** new `tests/test_driver_heartbeat.py` (or extend existing if any) covering:
- `OfferTadState` assembly from mock `offer_history` rows
- Lost Mode trigger conditions (each of the three narrative_blindness cases)
- `last_known_anchor_id` tracking across heartbeats
- JSONB serialization of `diagnostics.tad_verdicts`
- Round-trip: TadVerdict → JSONB → query → readable structure

Estimated test additions: ~12-15.

### Item 3d: Inferred-dropoff anchor recovery (Bible Rule 8)

**The problem:** stacked offer chains require a continuous anchor narrative. If ride A's dropoff fails to confirm spatially (cluster doesn't form, GPS drift, parking structure), ride B has no anchor — every subsequent ride falls into Lost Mode for the rest of the shift. Inferred-dropoff is the recovery scaffolding.

**Trigger conditions** — when pickup B's cluster commits via Rule 3 (Step 6 commit rule), AND ride A meets ALL of:
- `actual_pickup_at IS NOT NULL` (ride A's pickup confirmed)
- `actual_dropoff_at IS NULL` (ride A's dropoff never confirmed)
- current UTC time ≥ `ride_A.expected_dropoff_arrival_time`
- current UTC time ≥ `ride_B.expected_pickup_arrival_time` (sequential, not overlapping)

→ infer ride A's dropoff happened.

**Inferred values:**
- Inferred dropoff time = current UTC timestamp at pickup B's commit moment
- Inferred dropoff location, in priority order:
  1. **Primary**: last known cluster centroid before pickup B's cluster
  2. **Fallback** (no intermediate cluster): last GPS coordinate prior to cluster B formation. No accuracy threshold — chain math anchors on the odometer reading; the lat/lng is forensic context.

**Persistence — open design question, locks at implementation time:**

The Sprint Bible (Rule 8) explicitly deferred the persistence mechanism. Three candidate mechanisms; none ratified yet:

- **(a) New columns on `offer_history`**: `inferred_dropoff_at timestamptz`, `inferred_dropoff_lat double precision`, `inferred_dropoff_lng double precision`, `inferred_dropoff_source text` ("cluster_centroid" | "gps_fallback"). Schema migration required. Pros: tight coupling to the offer it describes, easy SQL queries. Cons: pollutes the schema with edge-case columns; Bible Section V mantra ("don't pollute schemas with transient diagnostic columns") tilts against this.

- **(b) Flag in `pudo_decision_context.tad_decision_context` JSONB**: store `inferred_dropoff_event` blob inside the existing JSONB column when inference fires. Pros: zero schema change, fits the Section V mantra. Cons: harder to query (JSONB path queries vs flat column SELECT), and `pudo_decision_context` is per-heartbeat — the inference fires once per chain, so storing it on every heartbeat post-inference is wasteful or requires careful "first occurrence" semantics.

- **(c) Separate event table `app_private.inferred_dropoffs`**: schema = `(offer_id, inferred_at, inferred_lat, inferred_lng, source, created_at)`. Pros: clean event log, easy joins to `offer_history`, append-only. Cons: another table, another migration, more cognitive load.

**Recommended path for next session:** open with this as the first ratification ask. Read Bible Rule 8's "Persistence requirement" paragraph, propose **(c) separate event table** to Gemini with the argument: clean separation of "what we knew at runtime" (heartbeat forensics) vs "what we inferred about a different ride after the fact" (durable correction). Gemini may push back; whatever consensus emerges locks the mechanism for this sprint.

**Differential trust note:** future Price Radar consumers may apply weight differences between spatially-confirmed vs inferred dropoffs. Whatever persistence mechanism wins, it must be queryable in a way that lets downstream code distinguish them. (a) and (c) make this trivial; (b) requires JSONB introspection.

---

## Findings to act on (READ BEFORE AUTHORING)

### Finding 1: TargetSpec.address is still dormant — Patch 2c name-witness contributes nothing in production

The Bible Finding 3 from the prior handoff still holds. `TargetSpec` has 4 fields (`lat, lng, address_class, named_roads`) — no `address` field. Item 2's `_signal_poi_match` uses `getattr(target, "address", "") or ""` which evaluates to empty string in production, short-circuiting the function.

**Consequence for Item 3b:** Lost Mode commit rule (Rule 3b) requires `outcome.poi_type_match is True`. That comes from Head 4 (`_signal_poi_type_match`), which uses `address_class` and IS live. So Lost Mode functionality is unaffected by the dormancy.

**Consequence for activation timing:** when `TargetSpec.address` lands (Phase 2c.3 or later), Patch 2c name-witness activates automatically, and Normal Mode's elevator clause gets a second corroboration source. No `where_am_i.py` change needed at that time — the defensive `getattr` is already in place.

**No action required for Item 3b.** Mentioned only because the next session may see empty-string `target_address` in forensic outputs and wonder why.

### Finding 2: Bridge state must be preserved by every change to `_evaluate`

Bridge state is the contract: when caller doesn't supply TAD context, `_evaluate` MUST behave identically to the pre-Item-3 version. The 513 test floor enforces this. Any modification to Step 4.5 / Step 5 skip / Step 6 commit rule that breaks bridge state will fire dozens of tests.

Specifically, `tests/test_scenarios.py` runs 69 TOML scenario replays end-to-end, all of them in bridge state (none supply TAD context). Don't break those.

### Finding 3: `evaluate_tad_gate` raises ValueError on naive datetime

`tad.py:439` enforces UTC at module entry: `cluster.latest.tzinfo is None` raises `ValueError`. The same holds for `compute_offer_expectations`'s `now` parameter and `prev_expected_dropoff_arrival_time`. v2.1 Section III is enforced at the boundary — caller is responsible for ensuring all datetimes are tz-aware UTC before passing them in.

**Consequence for Item 3b:** when assembling `OfferTadState` from `offer_history` rows, the `accepted_at`, `expected_pickup_arrival_time`, `actual_pickup_at`, `pickup_exit_time` fields all need to be tz-aware. Postgres `timestamptz` columns return aware datetimes when fetched via psycopg with the right cursor configuration — verify this on session open before authoring.

### Finding 4: The L-22 paste-safety hazard fired again this session

Twice in this session, my apply scripts hit edge cases that L-22 anticipates:

1. **Sentinel string with embedded escapes** — my Fix #1 patch declared an EXPECTED_SENTINEL containing `\"` escape sequences that didn't match the literal characters in the file. False-positive sentinel sweep failure (the file was correctly patched; only the verification check failed).

2. **Tristate-via-`None` confusion** — my classify table-test conflated "no verdict (bridge state)" with "verdict whose `.passed` is None (Lost Mode)". Pure logic bug, not a paste hazard, but the symptom looked like one at first.

**Both fixed via additional patches.** The lessons:
- Sentinel sweep strings should avoid escape sequences when possible; prefer ASCII-only short anchors.
- Tristate `Optional[bool]` types like `TadVerdict.passed` must be tested with explicit object construction, not boolean shorthand.

### Finding 5: 4 of the test fixture factories had mismatch potential

The existing fixture factories `_fake_pivot()`, `_topo()`, `_target()`, `_offer()` in `tests/test_where_am_i.py` have signatures that DON'T accept some kwargs you might guess from the production dataclasses they construct:

- `_fake_pivot()` — no `current_road_class` kwarg (production `RoadTopology` has it; the fake doesn't bother).
- `_topo()` — no `adjacent_roads` kwarg, no `current_road_class` kwarg (defaults via constructor).
- `_target()` — no `address` kwarg (TargetSpec doesn't have the field; Patch 2c is dormant).
- `_offer()` — no `pickup_minutes`/`trip_minutes` kwargs (Phase 1 fields default to None).

**L-6 corollary:** Read the existing factory signatures via `sed -n '<line>,<line>p'` BEFORE authoring code that calls them. Don't infer signatures from the production dataclass shape. This session burned two fix patches on this exact mistake.

---

## Locked contracts you'll consume (do not modify)

These are the in-memory shapes Item 3b builds on. None should change:

### `OfferTadState` (tad.py:315)
9 fields, all required, frozen. Caller assembles per offer.

### `TadVerdict` (tad.py:356)
6 fields, frozen. Returned by `evaluate_tad_gate` per offer. JSONB-friendly.

### `DiagnosticContext` (where_am_i.py:1268)
6 fields, frozen, `tad_verdicts` defaulted to `{}`. Returned by `evaluate_with_diagnostics`.

### `_commits` and `classify_commit_rule` (where_am_i.py)
Pure functions, exported. Stable signatures:
```python
_commits(outcome: MatchOutcome, verdict: Optional[TadVerdict]) -> bool
classify_commit_rule(outcome: MatchOutcome, verdict: Optional[TadVerdict]) -> str
```
Caller invokes `classify_commit_rule` when serializing committed matches into the JSONB blob.

### `evaluate_with_diagnostics` (where_am_i.py:1401)
6-arg signature, 4 new args optional with bridge-state defaults:
```python
def evaluate_with_diagnostics(
    self,
    driver_id: str,
    queue: list,
    per_offer_state: Optional[dict[str, OfferTadState]] = None,
    lost_mode: bool = False,
    last_known_anchor_id: Optional[str] = None,
    current_odometer: Optional[float] = None,
) -> tuple[list[WAIMatch], DiagnosticContext]
```

---

## Operational protocol reminders

- Paired programming: Claude proposes → Gemini reviews → consensus → execute
- Apply scripts in `~/puddlejumper-prod/tmp/` (gitignored)
- Migrations in `~/puddlejumper-prod/migrations/` (committed) with `YYYY-MM-DD_descriptive_name.sql` naming
- Recon files in `/tmp/` (Linux ephemeral)
- Pytest via venv: `source ~/puddlejumper-prod/venv/bin/activate` then `python3 -m pytest -x --tb=short`
- File transfers: download from chat UI, scp from laptop. md5 verify BEFORE running any apply script.
- L-3 envelope (Phase 1 verify+idempotency, Phase 2 in-memory transform with arithmetic gates, Phase 3 atomic write+readback+sentinel sweep). Use **dynamic delta computation** (`new.count("\n") - old.count("\n")`) rather than hardcoded constants — this session proved hand-counts were off by 2-10 lines per replacement and the dynamic computation caught it cleanly.
- L-3 sentinel sweep: avoid embedded escape sequences in sentinel strings. ASCII-only short anchors are the safe default.
- L-6 corollary: read factory/function signatures verbatim before calling them. This session burned two fix patches by inferring instead of reading.
- L-22 (chat paste-back rendering): if pasted output looks like `[name.py](http://name.py)`, it's chat-display linkification, not the actual terminal state. Read past it.

---

## Acceptance criteria for Items 3b + 3d + sprint completion

### Tests
- Pytest green: target ~525-535 (513 baseline + ~12-15 Item 3b tests + ~5-8 Item 3d tests)
- Bridge state regression check: all 513 existing tests still pass

### Deploy
- Single squash-merge commit (or clean commit chain) on `demolition-2026-05-04`
- Cloud Run deploy succeeds, traffic routes
- Smoke check via Bruno `driver_status` request returns no errors

### Forensic verification (1 hour post-deploy)
- New offers populate the four `expected_*` columns in `offer_history` (caller code assembling these at offer-receipt time)
- Cluster evaluations populate `tad_decision_context` JSONB blob in `pudo_decision_context`
- The JSONB structure matches the `dict[offer_id, TadVerdict]` shape — verify with `jsonb_path_query`
- No null-pointer errors in Cloud Run logs
- An inferred-dropoff event is observable when a real shift produces an unconfirmed dropoff followed by a confirmed pickup B (may take a multi-shift validation window)

---

## Cross-session resumption

If the next session pauses and resumes:

1. Read this handoff first
2. Read the Sprint Bible Rules 1, 3a, 3b, 5, 7, 8
3. Check git state: `git log --oneline f3f5dc9..HEAD`
4. Check pytest state: should be 513/513 green
5. Resume at the next unfinished item (3b first, then 3d)

The Phase 2 Claude Code work (`escape_detection.py`, `decisions/router.py` extension, `decisions/logger.py` extension, `poi_service.py` constant bump, `tmp/validate_phase_2c_2.py`) per the Sprint Bible's "Phase 2 spec" section is independent of Items 3b/3d and can ship in parallel or after.

---

End of handoff.
