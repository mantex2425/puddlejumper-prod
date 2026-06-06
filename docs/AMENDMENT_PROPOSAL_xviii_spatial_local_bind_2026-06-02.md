# Amendment Proposal — §XVIII Binding Paths: Spatial-Local Competing Set

**To:** Gemini (ratification), then CC (apply via L-3 envelope)
**From:** Claude + Andrew
**Date:** 2026-06-02
**Target file:** `docs/CANONICAL_RULES.md`, §XVIII.A → "Binding paths"
**Code status:** already deployed as `6140bbb` (`puddlejumper-api-00639-ccg`); validated 2026-06-02 (11/12 pickups bound, 91.7%, vs 1/7 on 2026-06-01). This amendment brings canon into sync with shipped+validated code — canon is currently behind the code.

---

## 1. The defect in the canon

§XVIII.A "Binding paths (2026-05-31 amendment)" path 2 defines the cold-start bind competing set as the **raw** alive-unpicked set, with the gate firing only when that set is a singleton:

> "when an FPO fires AND the heartbeat's alive-unpicked set is exactly `{action.offer_id}`"
> "Multiple alive-unpicked offers (`len(set) >= 2`) preserve the §XIV.I §5.3 ambiguity discipline (stay observation-only, defer narrative)."

On a stacked shift the raw alive-unpicked set is almost always ≥2 (cross-town offers parked in queue), so the singleton test fails and the bind is withheld. 2026-06-01 production: bind withheld on **6 of 7** pickup fires → ~80%-blind shift. The deployed fix (`6140bbb`) redefines the competing set as the **floor-clearing** alive-unpicked set; the canon text was not updated alongside it.

This is a **correction**, not an addition. It follows the canon's established supersession pattern (cf. §C.1/§C.2 "Status: historical").

---

## 2. The fix (deployed code, for reference)

`driver_heartbeat.py:~648`:

```python
pickup_floor_clearers = frozenset(
    str(offer_id) for offer_id, leg, outcome in diagnostics.per_target_outcomes
    if leg == 'pickup' and _commits(outcome, tad_verdicts.get(offer_id)))
local_competing = pickup_floor_clearers & alive_unpicked_offer_ids
if local_competing == frozenset({str(action.offer_id)}):
    queue.bind(action.offer_id, cur)
```

"Local" is **not** geometric — no radius, no H3, no geocode distance (preserves §XVI.D). "Local" = the offer cleared the pickup commit threshold at THIS heartbeat via `_commits` (which folds the TAD verdict in as an *input*, per §XVI.C, not as a gate).

---

## 3. Rationale — grounded in §0.D.4

§0.D.1/D.4 define a "candidate" as an offer **above the floor**. D.4: *"zero candidates above the WAI 0.40 floor"* → log miss, skip; D.1: multiple **high-confidence** candidates → record all. The regime is defined by floor-clearing, not by queue membership.

The global form counted offers that are **not candidates by D.4's own definition** (cross-town, below floor at this heartbeat) as if they contested narrative. That is a category error against the Prime Directive's existing vocabulary. The corrected predicate counts only floor-clearers — i.e., D.4 "candidates" — so the bind gate now conforms to a regime distinction §0 already establishes. This is not new policy; it is conformance.

Genuine same-curb ambiguity is preserved exactly: two or more offers clearing the pickup floor at the same location (`len(pickup_floor_clearers ∩ alive_unpicked) >= 2`) still stays observation-only per §XIV.I §5.3 / §0.D.4. The protection was never about queue size; it was about multiple *credible* candidates at the curb.

**Reconciliation with §C.4 / §H is unchanged.** The cold-start bind remains the lost-mode *exit* transition (it flips bit 1, ending lost-mode), not an in-lost-mode narrative commit. §C.4 ("observation-only during lost-mode") and §H ("refuse narrative commit on recovery") are untouched — the amendment changes only *which singleton* triggers the exit, not the principle that recovery-by-bind is the exit boundary.

**Single-source-of-truth contract preserved.** The bind gate still reads the shared `_get_alive_unpicked_offer_ids` (same source as `_detect_lost_mode`); it now *additionally* intersects that shared set with `pickup_floor_clearers` derived from the same heartbeat's `per_target_outcomes`. The shared alive-unpicked source — the thing the 2026-05-31 contract pins — is unchanged, so the bind/trigger non-drift guarantee holds.

---

## 4. Proposed canon edits

### 4a. Inline pointer in path 2 (minimal touch)

In §XVIII.A path 2, after the sentence ending "is exactly `{action.offer_id}`", insert:

> *(competing-set definition corrected 2026-06-02 — see "Spatial-local competing set" below; the set is the floor-clearing alive-unpicked intersection, not the raw alive-unpicked set)*

Leave the 2026-05-31 text otherwise intact (historical preservation per §C.1/C.2 convention).

### 4b. New dated sub-subsection (insert immediately after "Binding paths", before "Stale-pointer reconciliation")

```markdown
##### Spatial-local competing set (2026-06-02 amendment)

The §XVIII cold-start bind (path 2 above) tests for a singleton
competing set. As ratified 2026-05-31, that set was the RAW
alive-unpicked set, and the bind withheld whenever `len(set) >= 2`.

**Status of the raw-set form: superseded.** Measuring the competing
set globally counts offers that are not candidates by §0.D.4's own
definition. On a stacked shift, cross-town offers sit in the
alive-unpicked queue while scoring below the pickup commit threshold
at the current heartbeat — they are not credible candidates at this
curb, yet the raw-set form let them suppress the bind. 2026-06-01
production: the bind was withheld on 6 of 7 pickup fires (~80%-blind
shift) for exactly this reason.

**Post-amendment behavior.** The competing set is

    pickup_floor_clearers ∩ alive_unpicked_offer_ids

where `pickup_floor_clearers` is the set of offers whose pickup-leg
outcome `_commits` at this heartbeat (from the heartbeat's
`per_target_outcomes`, via `_commits(outcome, tad_verdict)`). The
bind fires iff this intersection equals exactly `{action.offer_id}`.

"Local" is a floor-clearing predicate, NOT geometric — no radius, no
H3 cell, no geocode distance (preserves §XVI.D). "Uncontested" is
measured by floor-clearer count at the curb, not by queue size.

Genuine same-curb ambiguity (≥2 offers clearing the pickup floor at
one location) still stays observation-only per §XIV.I §5.3 / §0.D.4 —
the protection against false binds is preserved, because that
protection was always about multiple credible candidates, never about
how many offers were parked in the queue.

This conforms the bind gate to §0.D.4: the competing set is now
exactly "candidates above the floor," which is the Prime Directive's
own definition of a candidate. The amendment is conformance, not new
policy.

The single-source-of-truth contract (above) is preserved: the bind
still reads the shared `_get_alive_unpicked_offer_ids`; it now
additionally intersects with `pickup_floor_clearers` from the same
heartbeat. The shared alive-unpicked source is unchanged.

**Validation (2026-06-02 stacked drive, 12 real pickups):** 11 of 12
bound narrative within 60s of the actual curb (91.7%), vs 1 of 7
(14%) on 2026-06-01 under the raw-set form. Validated against
driver-tapped contest-label ground truth (the engine bound the actual
stop, not its estimate). The single non-bind (offer 8845) had a blank
`wai_offer_id` — zero floor-clearers — and is the §0.D.4 log-and-skip
case (an upstream pickup-localization miss ~1mi off the true curb),
NOT a bind defect. Pinned by `tests/test_spatial_local_bind.py`.
```

---

## 5. Open flag for Gemini (do NOT fold into this amendment)

The handoff runbook's §2a note states the lost-mode WAI floor is **0.55**, while §XVIII.C.2 and §XIV.C state **0.40**. This amendment deliberately references `_commits` / "the pickup commit threshold" and bakes in **no number**, so it is correct regardless of which floor is canonical. But the 0.40-vs-0.55 discrepancy is a real inconsistency between the runbook and canon and needs its own resolution. Flagging, not resolving.

---

## 6. Separate companion change (different file — not this amendment)

The handoff doc's §2 **Q-VALIDATE** query is structurally broken (filters `planner_action LIKE 'Fire%'`, which always reads NULL `bound_post` because the bind fires on the *next* heartbeat). It returns 0/N regardless of fix state. The corrected WITH-clause query (bind-within-60s-of-`actual_pickup_at`) replaces it. That edit targets the **runbook/handoff doc, not `CANONICAL_RULES.md`** — tracked separately so the two changes don't entangle.

---

## 7. Ratification gates

1. Does Gemini accept the §0.D.4 grounding (floor-clearer = canonical "candidate"; global form is a category error against §0)?
2. Does Gemini accept the 8845-as-D.4-log-and-skip attribution (blank `wai_offer_id` ⇒ zero floor-clearers ⇒ prescribed skip, not defect)?
3. Does Gemini accept the supersession-with-historical-preservation form (4a inline pointer + 4b new dated sub-subsection) over an in-place rewrite of the 2026-05-31 text?

If all three clear → CC applies 4a + 4b via L-3 envelope (idempotency verify → in-memory transform → atomic write + read-back).
