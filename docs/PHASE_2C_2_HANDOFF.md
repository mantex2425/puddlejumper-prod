# Phase 2c.2 Sprint Handoff — Resume Brief

**For:** the next architecture-chat session continuing this sprint
**From:** the chat that just shipped TAD (commit `de2ba5d`)
**Date:** 2026-05-07
**Branch:** `phase-2c-2-tad-exit-4tools` off `f3f5dc9` on `demolition-2026-05-04`

---

## Read first (paste at session start)

Documents to load before any work:

1. **`docs/PHASE_2C_2_SPRINT_BIBLE.md`** — operative spec for this sprint. ~543 lines. Locked architectural rules, recon evidence, module specs. **READ END TO END.**

2. **`docs/SESSION_PROTOCOL.md`** — paired-programming workflow, paste-safety rules, lessons L-1 through L-22.

3. **`CANONICAL_RULES.md`** — eternal product law (coordinate functions, UTC, etc.).

4. **PuddleJumper Canonical Standards v2.1** — Andrew has this as a separate text. Section III (UTC-mandatory), Section IV (TAD as hard gate, distance primary, time asymmetric), Section V (forensic JSONB), Section VI (no Nail It buttons mandate). The TAD code already enforces Section III via ValueError on naive datetimes.

After loading docs, run a quick git recon:

```bash
cd ~/puddlejumper-prod && git log --oneline f3f5dc9..HEAD
```

You should see five commits ahead of base:
```
de2ba5d phase 2c.2 brain: tad.py Part 2 + Lost Mode (evaluate_tad_gate)
61094d0 phase 2c.2 brain: tad.py Part 1 (compute_offer_expectations)
386ba3f phase 2c.2 prereq: extend Offer with pickup_minutes/trip_minutes
7a236a3 schema: phase 2c.2 migration applied to prod
5be275e docs: phase 2c.2 sprint bible (operative spec)
```

If commits don't match, something has drifted — investigate before authoring.

---

## What's landed

### Schema (commit `7a236a3`)
Applied to production DB. **Do not re-apply.** Verify with:
```bash
psql -h 10.128.0.2 -U postgres -d puddlejumper -c "
SELECT column_name FROM information_schema.columns
WHERE table_schema='app_private' AND table_name='offer_history'
  AND column_name LIKE 'expected_%' OR column_name LIKE 'pickup_exit_%'
  OR column_name='exit_velocity_timeout'
ORDER BY column_name;"
```
Should return 7 rows.

```bash
psql -h 10.128.0.2 -U postgres -d puddlejumper -c "
SELECT column_name FROM information_schema.columns
WHERE table_schema='app_private' AND table_name='pudo_decision_context'
  AND column_name='tad_decision_context';"
```
Should return 1 row, jsonb.

### Offer dataclass extension (commit `386ba3f`)
`pudo_types.py` `Offer` now has `pickup_minutes: Optional[int]` and `trip_minutes: Optional[int]`. `driver_queue.py` `_project_offers` populates them from `offer_history`.

### `tad.py` (commits `61094d0` + `de2ba5d`)
867 lines. Public surface:
- `compute_offer_expectations(new_offer, prev_offer, current_odometer, now, ...)` → `Optional[OfferExpectations]`
- `evaluate_tad_gate(cluster, queue_offers, current_odometer, per_offer_state, lost_mode=False, last_known_anchor_id=None)` → `dict[str, TadVerdict]`

`TadVerdict.passed` is **tristate**:
- `True` = Normal Mode pass (distance in [85%, 115%] window)
- `False` = below 85%, skip candidate
- `None` = Lost Mode (`narrative_blindness` or `narrative_violation`)

Tests: 31/31 in `tests/test_tad.py`. Suite floor: **466/466**.

---

## What's left — Phase 1 (architecture-chat, this session)

Three deliverables. All require ratification discipline (Andrew runs through Gemini, consensus before commit).

### Item 1: Bible amendment for Lost Mode (small, do first)

The Bible currently documents the elevator rule (`weighted >= 0.90 OR (>= 0.80 AND poi_type_match)`) but not Lost Mode.

**What needs adding:**

- A new "Rule 7: Lost Mode" subsection in the architectural reconciliation section
- Update the `evaluate()` shape to show TAD now returns tristate (Step 4.5 already mentions TAD bouncer; just expand to note tristate)
- Update Step 6 Reduce to document the dual commit rule (Normal Mode rule + Lost Mode rule)
- A "Lost Mode triggers" subsection describing both `narrative_blindness` (caller-driven) and `narrative_violation` (per-offer overshoot >115%)
- Reference to the no-show / inferred-dropoff rule (see "Late ratifications" below)

**Authoring discipline:** small surgical str_replace edits to the existing Bible. Do NOT rewrite the whole doc.

**Acceptance:** Bible gets the new content, committed as `docs: phase 2c.2 sprint bible amendment for lost mode`.

### Item 2: Head 4 — `_signal_poi_type_match` + `CLASS_TO_TYPE_MAP` + `MatchOutcome` extension

In `where_am_i.py` (1391 lines pre-amendment). This is the new POI type-matching witness signal.

**Critical anti-drift discipline:**

- `_signal_poi_type_match` is a **witness signal**, not a weighted signal. It populates `MatchOutcome.poi_type_match` (boolean) and `MatchOutcome.poi_type_witness` (string). It is **NOT in `_CONFIDENCE_WEIGHTS`**.
- If you find yourself adding `poi_type_match` to `_CONFIDENCE_WEIGHTS`, **stop**. Re-read Bible Rule 2.
- Sibling pattern to existing `_signal_poi_match` (3-head, patch 2b). New function does POI **type** matching where existing does POI **name** matching.

**`CLASS_TO_TYPE_MAP` design:**

This is the part that needs an actual conversation. The map keys are the four address classes from `_CLASS_DISPATCH`:
- `"single_road"`
- `"intersection"`
- `"number_on_street"`
- `"apartment_complex"`
- `"poi"` (currently a stub matcher)

For each class, decide which Google Places types qualify as a valid match. Andrew has empirical scenarios from the 2026-05-06 audit (Excel Dental, Pappasito's at 10005 FM 1960, US-90 strip mall, Houston no-zoning false-positive cases) that should validate the choices. Walk through each before locking the map.

Reference: Google Places type taxonomy at https://developers.google.com/maps/documentation/places/web-service/place-types

**`MatchOutcome` extension:** add two fields after the existing `poi_match` / `poi_witness`:
```python
poi_type_match: Optional[bool] = None
poi_type_witness: Optional[str] = None
```

**Tests:** ~5 new tests covering Calhoun-style scenario (UH dropoff with university type match), Pappasito's-style scenario (street_number address with restaurant type match), Houston no-zoning false-positive (residential cluster with weak commercial POIs → returns False).

**Authoring discipline:** anchor-based apply scripts (mirror the gate-layer pattern). Read existing `_signal_poi_match` body before authoring `_signal_poi_type_match` so the conventions match.

**Acceptance:** all new tests pass, full suite stays green (target ~471), committed as `phase 2c.2 brain: head 4 (_signal_poi_type_match + CLASS_TO_TYPE_MAP + MatchOutcome)`.

### Item 3: `evaluate()` integration in `where_am_i.py`

The riskiest surgery in the sprint. Adds:

**(a)** Step 4.5 (TAD bouncer): after Map (Step 4) generates candidates, call `tad.evaluate_tad_gate(...)`. The caller assembles `OfferTadState` per offer from `offer_history` rows fetched alongside the queue.

**(b)** Lost Mode detection: caller decides when to set `lost_mode=True`. Triggers (per the late ratifications):
- No prior anchor available (queue empty / GC'd / stacked offer with no prev confirmation)
- Inferred-dropoff fallback (see below)

**(c)** Per-leg dispatch: only run spatial scoring for offers where `verdict.passed in (True, None)`. Skip offers where `verdict.passed is False`.

**(d)** Step 6 Reduce dual commit rule:
- `verdict.passed is True` → Normal Mode: commit if `weighted >= 0.90 OR (weighted >= 0.80 AND poi_type_match)`
- `verdict.passed is None` → Lost Mode: commit if `weighted >= 0.85 AND poi_type_match TRUE`

**(e)** Inferred-dropoff logic: when pickup B's cluster confirms (not when TAD evaluates — when commit happens), look back at ride A. If ride A has `actual_pickup_at` set but `actual_dropoff_at` IS NULL, AND `NOW() >= ride_A.expected_dropoff_arrival_time` AND `NOW() >= ride_B.expected_pickup_arrival_time`, infer that ride A's dropoff happened. Persist an inferred dropoff event (location = last known cluster before pickup B; time = `NOW()`). This unlocks ride B's TAD chaining for future stacked offers.

**(f)** Forensic blob assembly: build the `tad_decision_context` JSONB per the schema-documented structure, including TAD verdicts, spatial scores per signal, weighted_confidence, poi_type_match boolean, elevator_triggered or lost_mode flags, final_verdict ("COMMIT" | "SKIP").

**Authoring discipline:**

- Read `evaluate()` end-to-end before authoring
- L-6 corollary: grep ALL callers of `evaluate()` before changing its signature
- Write the integration as anchor-based patches with byte-delta verification
- Run pytest after each anchor patch

**Tests:** ~6 new integration tests. Specifically test the Lost Mode rule (mock a TAD verdict with `passed=None` and verify the stricter commit rule fires), the inferred-dropoff logic, and the forensic blob structure.

**Acceptance:** all integration tests pass, full suite stays green (target ~477), committed as `phase 2c.2 brain: evaluate() TAD bouncer integration + dual commit rule + inferred dropoff`.

---

## What's left — Phase 2 (Claude Code, separate session)

After Phase 1 ships, Claude Code takes over. Mechanical wiring against locked contracts.

1. `escape_detection.py` — new module, ~80 lines + 6 tests
2. `decisions/router.py` wiring — call `compute_offer_expectations()` at offer receipt
3. `decisions/logger.py` wiring — extend `offer_history` INSERT
4. `driver_heartbeat.py` wiring — call `check_exit_velocity()` post-pickup
5. `poi_service.py` — bump `API_SEARCH_RADIUS_M` from 50 to 150
6. `tmp/validate_phase_2c_2.py` — validation harness with 2026-05-07 shift + 2026-05-06 audit cases
7. Squash-merge to `demolition-2026-05-04`, deploy, smoke check
8. `docs/PHASE_2C_2_SPRINT_REPORT.md` — sprint completion report

The Bible already specifies all contracts. Claude Code reads the Bible + the brain commits and executes.

---

## Late ratifications (after the Bible was first authored)

These were ratified in chat but not yet folded into the Bible. Item 1 above (Bible amendment) handles all of these.

### Lost Mode commit rule
- Normal Mode: `weighted >= 0.90 OR (weighted >= 0.80 AND poi_type_match)`
- Lost Mode: `weighted >= 0.85 AND poi_type_match TRUE` (mandatory Head 4)

### 85% / 115% distance window
- < 85%: skip (driver hasn't arrived)
- 85%–115%: TAD passes (in window)
- > 115%: Lost Mode `narrative_violation` (driver overshot)

### Time alone does NOT trigger Lost Mode
Volatile signal. Captured in forensic blob but doesn't gate.

### Inferred-dropoff rule
When pickup B confirms AND ride A's dropoff is unconfirmed AND `NOW() >= ride_A.expected_dropoff_arrival_time` AND `NOW() >= ride_B.expected_pickup_arrival_time`, infer ride A's dropoff happened. Use last-known cluster before B as inferred dropoff location. Persist for forensic and for ride B's TAD chaining.

### No-show pickups still contribute Price Radar signal
Confirmed pickup at offer's text location → price binding is valid regardless of whether ride completed. Mirrors existing `community_offers` declined-offer weighting (0.3×). Phase 2g tunes the no-show weight.

### Manual Nail It is dev-only
Per v2.1 Section VI. Removed from production scope. Do not reference in commercial code paths.

---

## When is the sprint DONE?

The sprint is done when ALL of these hold:

### Code (architecture-chat + Claude Code)
- [ ] Bible amended with Lost Mode + 115% overshoot + dual commit rule + inferred-dropoff
- [ ] `_signal_poi_type_match` + `CLASS_TO_TYPE_MAP` + `MatchOutcome` extension shipped, tested
- [ ] `evaluate()` integration shipped: TAD bouncer + dual commit rule + Lost Mode detection + inferred-dropoff + forensic blob
- [ ] `escape_detection.py` shipped, tested
- [ ] `decisions/router.py` wired and tested
- [ ] `decisions/logger.py` wired and tested
- [ ] `driver_heartbeat.py` wired and tested
- [ ] `poi_service.py` API_SEARCH_RADIUS_M bumped to 150

### Tests
- [ ] Pytest green: target ~485-490 (435 baseline + ~50 sprint-added)
- [ ] Validation harness `tmp/validate_phase_2c_2.py` passes 100% on 2026-05-07 shift + 2026-05-06 audit cases

### Deploy
- [ ] Single squash-merge commit on `demolition-2026-05-04`
- [ ] Cloud Run deploy succeeds, traffic routes to new revision
- [ ] Smoke check via Bruno `driver_status` request: no errors, `expected_*` columns populated for new offers, `tad_decision_context` JSONB populated for cluster evaluations

### Forensic verification (1 hour post-deploy)
- [ ] `pudo_decision_context` rows include populated `tad_decision_context` JSONB
- [ ] `offer_history` rows for new offers include populated `expected_*` columns
- [ ] No null-pointer errors in Cloud Run logs
- [ ] At least one Lost Mode entry in JSONB (proves the path is reachable)

### Documentation
- [ ] Sprint completion report `docs/PHASE_2C_2_SPRINT_REPORT.md` authored covering: TL;DR, what landed file-by-file, Phase 1/2 split with commit SHAs, test results, deviations from Bible with rationale, pre-existing oddities flagged but not addressed, operational state (branch, commit, deploy revision), open decisions for Phase 2g, what's left for validation drives

### Validation drives (post-deploy)
- [ ] Andrew runs at least one Houston shift with the new code in production
- [ ] Forensic data captured to `pudo_decision_context` shows the system is firing as designed
- [ ] No regressions reported in existing PUDO accuracy
- [ ] At least one PUDO commits via Lost Mode in real driving conditions (proves the path is operationally useful, not just code-reachable)

When all boxes check, the sprint is done. **Phase 2g (threshold tuning, additional spatial tools, etc.) is a separate sprint.** Do not scope-creep tuning into Phase 2c.2.

---

## Resumption order

1. Read this handoff
2. Load Bible + Session Protocol + Canonical Rules + v2.1 Standards
3. Run git log to verify branch state matches "What's landed"
4. Run pytest to verify 466/466 baseline
5. Start with Item 1 (Bible amendment)
6. Then Item 2 (Head 4)
7. Then Item 3 (evaluate() integration)
8. Hand off to Claude Code with a kickoff message pointing at this branch + the amended Bible

If anything in "What's landed" doesn't match reality, **stop and reconcile before authoring.** Drift between this doc and the actual repo state is a clear signal something happened we weren't expecting.

---

## Operational protocol reminder

- Paired programming: Claude proposes → Gemini reviews → consensus → execute
- Apply scripts in `~/puddlejumper-prod/tmp/` (gitignored)
- Migrations in `~/puddlejumper-prod/migrations/` (committed) with `YYYY-MM-DD_descriptive_name.sql` naming
- Recon files in `/tmp/` (Linux ephemeral, distinct from project tmp)
- Pytest via venv: `source ~/puddlejumper-prod/venv/bin/activate` then `python3 -m pytest -x --tb=short`
- File transfers: Andrew uses chat-UI download then manual scp from laptop. The `scp /mnt/user-data/outputs/...` pattern fails on his VM (mount is in Claude's sandbox, not VM)
- Never paste markdown blockquotes (`> `) to bash; never paste multi-line markdown to bash heredocs

---

End of handoff.