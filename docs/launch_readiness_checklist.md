# PuddleJumper Launch Readiness Checklist

## Open blockers / known gaps (2026-06-01)

- **P0 — §XVIII bind ambiguity is GLOBAL, must be SPATIAL-LOCAL.** Confirmed root
  cause of the 2026-06-01 all-lost-mode shift (only 1 of 7 pickup fires bound).
  The cold-start bind gate counts the whole alive-unpicked queue instead of the
  offers competing for the current cluster. Fix = leg-matched WAI-floor-cleared
  local competing set (delegates to _commits). Proposal ratified Claude+Gemini:
  docs/FIX_PROPOSAL_BIND_SPATIAL_LOCAL_2026-06-01.md. Recon:
  docs/RECON_PICKUP_NOBIND_2026-06-01.md. STATUS: CC implementing; not deployed.

- **P1 — Re-validate multi-PUDO scenario suite against the post-demolition engine.**
  Hot-swap (dropoff + pickup same curb), round-trip (pickup + dropoff same geocode,
  same offer), all §XIV.I §5.3 cases. Original scenarios predate the state-machine
  demolition and use stale STACKED/ENROUTE vocabulary. SEQUENCE AFTER the bind fix —
  several run through the dispatch path the bind fix touches, and the suite is the
  regression surface that proves the bind fix didn't break hot-swap. Round-trip is
  COUPLED to the deferred retired-leg item (below). Inventory:
  docs/SCENARIO_INVENTORY_AUDIT.md (PENDING — not yet generated).

- **P2 — Retired-leg re-scoring (deferred per bind-fix §4.3).** WAI keeps scoring an
  offer's pickup leg after actual_pickup_at is written (8739 fired 4x FPO on
  2026-06-01). The bind fix insulates the bind decision via the & alive_unpicked
  intersection, but does NOT stop the re-scoring. KNOWN COUPLING: this is the exact
  mechanism that breaks round-trip dropoff detection — a round-trip's second arrest
  re-scores pickup instead of advancing to dropoff. Must be resolved before round-trip
  validation (P1) can pass. Separate matcher recon, not yet investigated.

- **P2 — Scenario classifier (text) + arrest-sequence detection (physics) — DESIGN
  CANDIDATE.** Text classifier at offer-receipt from offer-card address STRINGS only
  (never geocodes; §V text is the only Uber-given data; §XVI.D coordinates untrusted):
  labels ROUND_TRIP and EXACT_SHARED_CURB via canonicalized address match (leans on
  a05716b). Classifier ANNOTATES, never gates. EXPLICIT NON-GOAL: text cannot detect
  same-block / across-the-street hot-swaps (distinct address strings); geocode-proximity
  detection FORBIDDEN (§XVI.D). Same-block hot-swap is an ARREST-SEQUENCE phenomenon
  for the matcher/dispatch layer, not the predictive classifier. SPEC AFTER bind fix +
  retired-leg land. Needs Claude->Gemini ratification before code.

- **P2 — Dropoff-clear behavior in lost-mode (recon candidate).** Gemini's "dropoff is
  narrative-shielded" premise is FALSE when current_offer_id is NULL (the common
  lost-mode state) — 8740/8748 fired dropoff observations in lost-mode on 2026-06-01
  with no narrative shield. Not a bind bug (dropoff clears, doesn't bind), but possible
  wrong-clear / wrong-observation risk against a global candidate set. Recon, not yet
  investigated.
