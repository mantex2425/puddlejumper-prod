# Phase 2c.2 Sprint — Cutover Handoff (2026-05-09 third session, post-commit-1)

**Branch:** `phase-2c-2-tad-exit-4tools` @ HEAD `f071d11`
**Last push:** origin still at `860a9c0` — local is **1 commit ahead, not pushed yet**
**Pytest floor:** 558/558 (commit 2 will raise to 564)
**Cloud Run:** `puddlejumper-api-00598-hvx`, 100% traffic — schema migration applied, no deploy yet

---

## Read order at session start

1. **This document** — operative state and exact next actions
2. `docs/SPRINT_STATE_HANDOFF_2026-05-09_v2.md` — second session's resurrection-validation handoff (already partially superseded by tonight's combined sprint)
3. `docs/SESSION_PROTOCOL.md` — paired-programming workflow, paste hazards
4. `docs/CANONICAL_RULES.md` — eternal architectural rules (Rule V especially relevant to this sprint)

---

## Recon at session start

```bash
cd ~/puddlejumper-prod && \
{
  echo "=== branch + commits ahead of origin ==="
  git status -sb
  echo ""
  git log --oneline origin/phase-2c-2-tad-exit-4tools..HEAD
  echo ""
  echo "=== confirm commit 2 apply script is on the VM ==="
  ls -la tmp/apply_phase_2c_2_commit2_poi_wiring.py
  echo ""
  echo "=== confirm wai_status column is gone (commit 1 migration applied) ==="
  psql -h 10.128.0.2 -U postgres -d puddlejumper -c "\d app_private.pudo_decision_context" | grep -c wai_status
  echo ""
  echo "=== pytest baseline ==="
  source venv/bin/activate && python3 -m pytest --tb=short -q 2>&1 | tail -5
} > /tmp/recon_session_start.txt && cat /tmp/recon_session_start.txt
```

Expected:
- `f071d11 phase 2c.2: excise wai_status column and consumer` — local 1 ahead of origin
- Apply script present at ~25KB
- `wai_status` grep count: 0 (column gone)
- Pytest: 558/558

If any are off, triage before proceeding.

---

## Sprint context — what just shipped, what's next

Combined sprint with two ratified commits and one push:

- **Commit 1 (DONE, locally committed as `f071d11`):** Excise `wai_status` column from `pudo_decision_context` schema and remove the three references from `driver_status.py`. Migration applied to live DB. Pytest 558/558 unchanged.
- **Commit 2 (PENDING):** Wire POI forensics. Three NULL-hardcoded columns (`poi_lookup_source`, `poi_match_score`, `poi_top_names`) become populated from `MatchOutcome.poi_witness`/`poi_match` and a new `DiagnosticContext.cluster_poi_names` field. Adds 6 tests (558 → 564) to a new test file establishing baseline pytest coverage for `_log_decision_context`.

Then: ONE push, deploy, validation drive.

The architectural framing for this sprint (and a Ratification record) is in `PHASE_2C_2_FORENSIC_WIRING_PROPOSAL_v2.md` — Andrew's local download. Gemini ratified all four asks: Path A (drop wai_status) re-confirmed, two-commit structure ratified, L-6 process correction formalized, architectural direction (excision over preservation) confirmed as the new default post-demolition.

---

## Exact next steps

### Step 1 — Run commit 2 apply script

```bash
cd ~/puddlejumper-prod && python3 tmp/apply_phase_2c_2_commit2_poi_wiring.py 2>&1 | tee /tmp/commit2_apply.log
```

The script touches three locations:

- `where_am_i.py`: 5 patches — extends `DiagnosticContext` with `cluster_poi_names`, populates from `cluster_pois` in both success and except branches, threads through both construction sites
- `driver_heartbeat.py`: 1 patch — replaces 3 NULL bindings in `_log_decision_context` with real expressions
- `tests/test_log_decision_context_bindings.py`: NEW FILE — 6 tests, ~390 lines, mock-cursor pattern per memory #13

Expected output: `APPLY COMPLETE`, all post-patch sentinels confirmed present.

### Step 2 — Run pytest

```bash
source venv/bin/activate && python3 -m pytest -x --tb=short
```

**Target: 564 passed.**

If pytest fails, the most likely failure mode is the `COL` index map in the new test file — Claude flagged this honestly during authoring. The map maps column names to positions in the INSERT params tuple based on column-list order in `_log_decision_context`. If the map is off, the regression-guard test fails with values shifted by 1-2 positions.

If that happens:

```bash
# Reveal the exact column order:
sed -n '683,720p' driver_heartbeat.py
# Count the columns in the column list (between INSERT INTO ... ( and ) VALUES)
# Update tests/test_log_decision_context_bindings.py COL dict accordingly
```

The new test file is structurally simple — fix is editing the `COL = {...}` dict at the top, no other changes needed.

### Step 3 — Stage and commit commit 2

```bash
git add where_am_i.py driver_heartbeat.py tests/test_log_decision_context_bindings.py
git commit -m "phase 2c.2: wire POI forensics into pudo_decision_context

Populate the three POI columns (poi_lookup_source, poi_match_score,
poi_top_names) by surfacing existing data from MatchOutcome and a new
field on DiagnosticContext. Replaces the Phase 1B placeholder NULL
bindings.

Changes:
  - where_am_i.py: extend DiagnosticContext with cluster_poi_names
    field (default_factory=list, mirrors tad_verdicts pattern from
    Item 3e). Populate in _evaluate() after cluster_pois projection
    as top-3 by distance, formatted 'Name (Xm)' per Gemini's psql-
    scannability ratification.
  - driver_heartbeat.py: replace 3 NULL placeholders in
    _log_decision_context with real expressions sourced from
    top_outcome.poi_witness, top_outcome.poi_match, and
    diagnostics.cluster_poi_names (empty list coerces to NULL).
  - tests/test_log_decision_context_bindings.py (NEW): 6 tests
    establishing baseline pytest coverage for _log_decision_context.
    Recon revealed zero pre-existing pytest coverage for the writer;
    this file is the unit-level floor going forward. Includes a
    regression-guard test asserting existing wai_* bindings continue
    to populate (insurance against adjacent-line str_replace damage).

Pytest: 558 -> 564.
No behavior change to any matcher or gate. Pure forensic plumbing —
the system makes the same decisions before and after; we just see
more about why."
```

### Step 4 — Push both commits

```bash
git push origin phase-2c-2-tad-exit-4tools
```

Pushes `f071d11` (commit 1) and the new commit 2 in one go. Origin now at HEAD.

### Step 5 — Deploy

```bash
bash deploy.sh
```

Verify new Cloud Run revision becomes live and serves 100% traffic. Watch for errors in the first ~5 minutes.

### Step 6 — Validation drive

Drive a short loop with at least one offer in queue. Once back home, run:

```sql
SELECT
  COUNT(*) AS pdc_rows,
  COUNT(poi_lookup_source) AS poi_source_populated,
  COUNT(poi_match_score) AS poi_score_populated,
  COUNT(poi_top_names) AS poi_names_populated,
  COUNT(wai_target_address) AS wai_target_populated  -- regression check
FROM app_private.pudo_decision_context
WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND created_at > '<post-deploy-timestamp>';
```

Expected:
- `pdc_rows` ≥ 1
- POI columns populated for rows where a `top_match` existed
- `wai_target_populated` continues populating (regression check)

Then sanity-check a specific row to see real Houston data:

```sql
SELECT
  cluster_lat, cluster_lng, cluster_size,
  wai_pudo_type, wai_confidence, wai_target_address,
  poi_lookup_source, poi_match_score, poi_top_names
FROM app_private.pudo_decision_context
WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND created_at > '<post-deploy-timestamp>'
  AND wai_pudo_type IS NOT NULL
ORDER BY created_at DESC
LIMIT 5;
```

This is the moment the monitors light up. We see real WAI evaluations alongside their POI provenance for the first time. From here, X3 (the auto-nailer firing investigation) becomes prosecutable.

---

## What the apply script does NOT do — one risk to know

The new test file `tests/test_log_decision_context_bindings.py` builds a `COL` index dict mapping column names to positions in the INSERT params tuple. Claude built this from the column list visible in recon (`driver_heartbeat.py:683-700`) but did NOT verify every index by counting bytes manually. If the COL map is off, the regression-guard test fails with values shifted by 1-2 positions.

**Diagnostic signal:** test failure where the assertion error shows a value that "looks almost right but in the wrong place" — e.g., `assert "pickup" == 0.85` (string from one column position vs. float from another).

**Fix:** look at `driver_heartbeat.py` lines 683-720 — count columns in the INSERT column list (between `INSERT INTO ... (` and `) VALUES (`), then update the `COL = {...}` dict in the test file. No other changes needed. Pytest re-run validates.

If pytest is green on first try, the COL map is correct. If it's red, this is the most likely cause and the cheapest first thing to check.

---

## State machine residue note (architectural framing)

This sprint formalized something larger: PuddleJumper is **event-detection, not state-tracking**. The auto-nailer is a detection event that fires once per pickup, drops a `community_offers` row, drops a price-radar row, triggers a voice utterance. There is no canonical "driver state" because the product doesn't need one. PUDO occurrence is recorded on `offer_history` via `actual_pickup_at` / `actual_dropoff_at` timestamps, not on a state object.

`/driver/status` and the web monitor at `app.puddlejumper.io/monitor` are state-machine-era residue from before the demolition. Andrew has confirmed the monitor is knowingly broken-by-attrition pre-launch; rebuild is post-launch work. Future sprints may excise more state residue (`armed`, `target_type`, `stopped_seconds`, `last_3_dispatch_actions`) but each as work surfaces them, not pre-emptively.

Gemini ratified this direction. Future state-machine residue is now **default-deletable** rather than default-preserved. The Option H1 "honest representation of 'this concept is gone'" comment in `driver_heartbeat.py:813-820` is a transitional artifact that can be cleaned up whenever a future sprint touches that file.

---

## What this sprint was NOT — and what's next

Out of scope this sprint:

- **TAD anchor GC defect** in `decisions/logger.py:88-105`. Still real, design ratified pending Gemini final review. Three-rule SQL filter: declined offers don't anchor / expired ACCEPTs don't anchor / completed rides don't anchor.
- **Why aren't auto-nailers firing?** `actual_pickup_at` and `actual_dropoff_at` are NULL on every recent row. This is the real production blocker for launch. With this sprint's forensic wiring, the question becomes prosecutable.

The next sprint after this one (X3) is "auto-nailer fire investigation." With POI columns lit up and `tad_decision_context` JSONB populated, the path is: drive a real ride with offers in queue, look at PDC rows for the cluster that should have committed, see what TAD said, see what WAI confidence was, see what POI provenance was. Then either prosecute the GC fix (if TAD held it back) or surface the deeper defect (if something else is wrong).

---

## Operational reminders

- **L-6 corollary "second strike" floor:** repo-wide `grep -rn "<symbol>" --include="*.py" --include="*.sql"` BEFORE drafting any proposal involving rename/signature change. Not just before the apply script. Tonight's draft revision was caused by skipping this — the discipline now applies at proposal-authoring time.
- **L-3 envelope discipline:** every apply script in `~/puddlejumper-prod/tmp/`, never `/tmp/`.
- **Paste hazards (L-22):** chat-display linkification of `[name.py](http://name.py)`. File transfer via `create_file` → `present_files` → Andrew's local-then-scp pattern. `scp /mnt/user-data/outputs/<file>` from the VM fails — that mount is in Claude's sandbox.
- **Test floor: 558 entering session, 564 after commit 2.** Every change must verify against full pytest run.
- **Pre-flight rollback:** `~/puddlejumper-prod/tmp/pdc_schema_backup_2026-05-09.sql` is the schema dump from before commit 1. If something post-deploy goes catastrophically wrong, this restores `wai_status` (though no data restoration needed since it was always NULL).

---

## Useful queries for next session

After validation drive completes, additional health-check queries:

```sql
-- Are POI lookups happening at all (resurrection patch still working)?
SELECT COUNT(*) AS rows_with_poi_data
FROM app_private.pudo_decision_context
WHERE created_at > '<post-deploy-timestamp>'
  AND poi_top_names IS NOT NULL;

-- Distribution of POI lookup source types (witness format provenance)
SELECT
  CASE
    WHEN poi_lookup_source LIKE 'fuzzy:%' THEN 'fuzzy'
    WHEN poi_lookup_source LIKE 'branded:%' THEN 'branded'
    WHEN poi_lookup_source LIKE 'airport_type:%' THEN 'airport'
    WHEN poi_lookup_source IS NULL THEN 'no_match'
    ELSE 'other'
  END AS source_type,
  COUNT(*) AS rows
FROM app_private.pudo_decision_context
WHERE created_at > '<post-deploy-timestamp>'
GROUP BY 1
ORDER BY rows DESC;

-- For X3: did any heartbeat have a high-confidence WAI outcome that
-- did NOT result in a fire? (Smoking gun for auto-nailer silent-fire bug.)
SELECT
  created_at, cluster_lat, cluster_lng,
  wai_pudo_type, wai_confidence,
  poi_match_score, poi_lookup_source,
  dispatch_executed, dispatch_error,
  tad_decision_context
FROM app_private.pudo_decision_context
WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND created_at > '<post-deploy-timestamp>'
  AND wai_confidence >= 0.80
  AND dispatch_executed = false
ORDER BY wai_confidence DESC
LIMIT 20;
```

That last query is the X3 starter pack — the heartbeats most likely to reveal where the silent-fire bug lives.

---

End of handoff.
