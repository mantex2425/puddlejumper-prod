# HANDOFF — §7 code complete; next: DSI merge + deploy, then Step 7 (9132 replay)

**Date:** 2026-06-05
**Branch:** `fix/restore-fire-error-metric-2026-06-02` (HEAD `2418401`)
**Status:** §7 odometer/band/orphan code sequence COMPLETE. Full floor 753 passed,
1 skipped, 0 failed. Next work is NOT code — it's deploy + a fresh forensic replay.

---

## What was committed this session (all on the branch above)

- `1f5a65e` — Step 4: odometer single-owner accessor + PDC persistence.
  **DEPLOYED** as revision `00643-5bv` (proven live, row 427571).
- `2064ea6` — FINDING ERRATUM: struck the phantom "bridge term"; the real 9132
  mechanism was the liveness predicate's wrong-column bug. Pinned the band.
- `066f8ab` — Step 6 band PRIMITIVE in `pudo_types.py` (`odometer_band`,
  `odometer_in_band`; `ODOMETER_BAND_TOLERANCE_PCT=0.15`,
  `ODOMETER_BAND_NOISE_FLOOR_MI=2.0`). Single arithmetic owner.
- `aa4eada` — Step 6 piece (i): `LIVE_OFFER_PREDICATE_SQL` rewritten onto the
  two-leg band. Equivalence-gated, arity-balanced.
- `4f777fe` — clean-slate recon (band confirmed single-owner).
- `b5399b3` — Step 6 piece (ii): evicted the vestigial raw_min/window_min
  time-axis machinery + 4 dead MINUTES constants. Preserved
  `GC_ABANDONMENT_CEILING_HOURS=4` (the live 4h backstop) + provenance comments.
- `d0bd0a3` — TAD-unification Option A: drift-guards, NO refactor. TAD's
  candidacy gate and the liveness band are SEPARATE modules sharing arithmetic
  (short-trip policy diverges; tristate is irreducible). Added the 0.15
  drift-guard test + independence comments at the two coincidentally-equal 2.0
  sites.
- `2418401` — Step 5 Option C: deleted the redundant orphan re-check in
  `tad.compute_offer_expectations`. The orphan/GC decision belongs SOLELY to the
  upstream Horizon Budget GC in `decisions/logger.py` (proven subordinate via
  the guards at logger.py:170/249). `logger.py` UNTOUCHED (its `time_exceeded`
  is the orphan-chaining decision's designed odometer-dark fallback, a different
  mechanism from the reaper's retired time ceiling).

---

## CRITICAL — deploy state

**Production currently serves `00644-w4s` (the DSI revision), which contains NONE
of this session's work** except Step 4 (which shipped earlier in `00643-5bv`,
now superseded by `00644-w4s` — VERIFY whether `00644-w4s` carries Step 4's
logging; it was built on `feat/dsi-v1`, a parallel branch).

The branch `fix/restore-fire-error-metric-2026-06-02` (8 commits above) has NOT
been deployed. To get this work live it must be **merged with `feat/dsi-v1`**
(the DSI branch prod is currently built from), conflicts resolved, then deployed
to Cloud Run `puddlejumper-api` in `us-central1` (project `puddle-jumper-477316`).

---

## NEXT WORK (fresh thread)

### 1. DSI merge + deploy (gated: do this FIRST)
- Merge `fix/restore-fire-error-metric-2026-06-02` with `feat/dsi-v1`. Recon the
  DSI branch state first — it was built in a parallel CC session and its overlap
  with the odometer/band/orphan files (`tad.py`, `pudo_types.py`,
  `driver_queue.py`, `decisions/logger.py`) is UNKNOWN and must be checked for
  conflicts before merging.
- Deploy the merged revision. Confirm it carries BOTH Step 4's odometer logging
  AND the Step 6 band (Step 7 depends on both being live).

### 2. Step 7 — replay 9132 against the DEPLOYED merged revision
- This is a FRESH forensic investigation, blocked on the deploy above.
- The question: with the corrected liveness predicate (band, piece i) AND Step
  4's odometer logging both live, replay offer 9132 and get the UNAMBIGUOUS
  verdict on what reaped it. The erratum (`2064ea6`) established the mechanism
  was the liveness predicate's wrong-column bug (consumed
  `miles_at_offer_receipt` instead of `expected_pickup_distance`), NOT the
  phantom bridge term. Step 7 confirms this against the deployed fix.
- Driver ID: `UjT1hE9eBXh2q95aSZYOkzDJ8lo1`. Forensic data in
  `pudo_decision_context` / the decision_log + offer_history tables.

---

## Key docs (source of truth, all in docs/)
- `FINDING_ODOMETER_GC_ERRATUM_2026-06-05.md` — the corrected law (bridge term
  struck; band pinned; the real 9132 mechanism).
- `RECON_ODO_CLEAN_SLATE_2026-06-05.md` — the single-owner audit.
- `CANONICAL_RULES.md` — note §5.3 there is **Asymmetric Ambiguity Handling**
  (two pickups/dropoffs at one geocode), NOT time-gate retirement. The
  time-ceiling §5.3 is in the FINDING and was about the LIVENESS REAPER, not the
  Horizon Budget GC. (This collision cost a detour in the Step 5 session;
  recorded so it doesn't recur.)

---

## Non-blocking doc-hygiene carried forward (any-time sweep)
- `RIDE_LIFECYCLE.md` §3 — stale time-window retirement.
- `INDEX.md` — odometer-subsystem entry + erratum + primitive lines.
- `tests/test_offer_history_anchors_live.py:44-45` — docstring raw_min/window_min
  reference (stale, harmless).
- `decisions/logger.py` chain-horizon 1.25 buffer — last surviving 1.25; future
  cross-subsystem consistency note (independent, documented).

---

## Standing protocol reminders (for the fresh thread)
- Claude proposes → Gemini ratifies → CLI/psql apply on VM (`andrew@puddle-jumper`,
  repo `~/puddlejumper-prod`). `scp` runs on the Mac (pushes to VM); apply +
  pytest + git run on the VM (the venv/Python live there — running on the Mac
  gives "command not found").
- L-3 idempotent apply envelope for all code changes (Phase 1 anchor/idempotency,
  Phase 2 transform+invariants, Phase 3 atomic write+readback; exit 0/2/3/6).
  This session the envelope correctly refused to write 4 times (false-matched
  invariants, ambiguous anchors, stale files) — disk never corrupted.
- Paste-safety: verify reconstructed anchors against the file (cat -A) BEFORE
  transfer; substring `.count()` can false-match comments/docstrings — use
  line-anchored or multi-line anchors. Re-download from the file link after any
  edit (stale `~/Downloads` caused a re-apply detour).
- Full test floor after every code change — it caught the Step 5 dual-authority
  regression that the narrower per-file checks missed.
