# PuddleJumper Technical Debt Log

**Status:** tracked work that is known-correct-but-not-yet-clean.
**Discipline:** entries are added when a doctrine deviation or naming
mismatch is accepted as a scoped trade-off for shipping focus. Each entry
names the deferred work, the reason for deferral, and the suggested
window for cleanup.

This is separate from `CANONICAL_RULES.md` (eternal product law) because
debt items are by definition not eternal — they exist to be paid down.

---

## Phase 3: Lexical Alignment

### `Offer.accepted_at` → `Offer.created_at`

**Status:** accepted as Phase 2 asymmetry (2026-05-13). Tracked here for paydown.

**The lie.** `pudo_types.Offer.accepted_at: datetime` carries the value of
`offer_history.created_at`. No `accepted_at` column exists on the table.
The dataclass field is named wrong; its docstring claims it comes from
`offer_history.accepted_at` (which doesn't exist).

**Provenance.** Sub-step 1b.1, commit `6e1d60f`. The `accepted_at` name
was introduced when the field was added to support `accepted_at_anchor`
semantics in WAI's cluster-revisit topology check. The schema was assumed
to have an `accepted_at` column; it didn't, so the SQL was written as
`accepted_at=o['created_at']` in `driver_queue._project_offers`. The
mismatch has persisted since then.

**Where it surfaces.** Phase 2 of the Observation-Before-Narrative work
(commit landing 2026-05-13) builds `OfferMeta(created_at=offer.accepted_at)`
at two callsites. The asymmetry between the field names is annotated at
the callsites with comments referencing this debt entry.

**Consumers to update on cleanup.** Per the 2026-05-13 L-6 grep:

- `pudo_types.py:82` — dataclass field definition and docstring
- `driver_queue.py:543` (approx) — the `accepted_at=o['created_at']` line
- `where_am_i.py:1653` — `accepted_at_anchor = min(offer.accepted_at ...)`
- `tests/test_driver_heartbeat_3b_r.py:195,237` — `assert s.accepted_at.tzinfo == UTC`
- `tests/test_where_am_i.py:1229,1707,1721` — three references
- Plus comments / progress docs in `apply_substep_1b1.py`,
  `apply_substep_1b2.py`, `apply_v26_amendment.py`, and several
  `refresh_progress_*.py` files (historical; lower priority to update)

Variable names like `accepted_at_anchor` in `where_am_i.py` should be
renamed in lockstep — keeping a variable named `accepted_at_anchor` that
holds the value of a renamed `created_at` field would just relocate the
lie.

**Suggested cleanup window.** After Houston Playback v2 is green at the
end of Phase 5. At that point we have a tested regression net for the
dispatch behavior, so any test failure during the rename is provably
naming-related, not logic-related. Gemini's framing: "the Playback Shield."

**Cleanup tracking.** When this debt is paid, this section moves to a
closed-debt log at the bottom of this file with the cleanup commit hash.
