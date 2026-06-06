# HANDOFF — Review the 2026-06-04 PM Drive (first live drive on the adjacency fix)

**Purpose:** Thorough forensic review of today's (June 4) PM driving session and its PUDO
results. This is the FIRST live drive on production revision `00642-fg7`, which carries the
residential-adjacent re-weight shipped 2026-06-03. The review's central question: **did the
adjacency fix recover residential-adjacent pickups in the wild, and did it cause any new
problem (especially the ×0.888889 haircut on non-adjacent matches)?**

**Read first, in this order:**
1. `docs/FINDING_residential_adjacent_pickup_below_floor_2026-06-03.md` — the fix that shipped
   yesterday. §4.6 (as-shipped outcome) and the "×0.888889 haircut" note are the things to
   validate today. §2.4 explains the weight mechanism; §3 the frequency basis.
2. This handoff.

---

## 0. WHAT SHIPPED YESTERDAY (the thing under test today)

- **Change:** `_CONFIDENCE_WEIGHTS["intersection"]` in `where_am_i.py`, `adjacent_road_match`
  0.10→0.20, other six weights ×0.888889 (sum preserved 1.0). Commit `9eebb45`, rev `00642-fg7`.
- **Intended effect:** residential pickups where the driver stops on the street ADJACENT to a
  geocoded intersection now clear the 0.40 WAI floor (previously capped ~0.29, ~14% of arrested
  legs were lost to this).
- **Known structural cost to watch:** every intersection match with `adjacent_road_match=0`
  (the non-adjacent case) takes a flat 11.1% confidence haircut (`new = old × 0.888889`).
  Yesterday's 55-leg demotion sweep showed DEMOTED=0 (no real leg fell below floor), but a
  non-adjacent match that naturally sits near 0.45 would now land near 0.40. **A non-adjacent
  intersection pickup newly failing at ~0.40–0.45 today would be this haircut, not a new fault.**

---

## 1. SESSION BOUNDS (how to scope today's data)

- **Drive-start marker:** `driver_trip_state_log` row, `to_state='DRIVE_START_MARKER'`,
  `trigger_event` like `manual_marker_pre_drive_pm_2026-06-04_...`, set at
  **2026-06-04 09:04:25 UTC**. Use as the lower bound.
  - NOTE: 09:04 UTC = 04:04 CT. If the PM drive actually started later, the marker may bound an
    AM+PM span — check `driver_trip_state_log` for any later marker and scope accordingly.
- **Driver:** `UjT1hE9eBXh2q95aSZYOkzDJ8lo1`
- **Upper bound:** end of today's driving (open-ended; use NOW() or last heartbeat).
- All times in `pudo_decision_context.created_at` are UTC; localize to America/Chicago at
  display only (`AT TIME ZONE 'America/Chicago'`).

---

## 2. SCHEMA NOTES (verified 2026-06-04 — do NOT re-guess these)

`pudo_decision_context` real columns (the offer id is NOT a bare `offer_id` and `leg` is NOT
top-level — both cost queries this session):
- Offer id lives in: `current_offer_id`, `primary_offer_id`, `wai_offer_id`,
  `matched_offer_id`, `current_offer_id_at_eval` — there is **no** `offer_id` column.
- `leg` (pickup/dropoff) is **inside** `wai_per_offer_scores` (jsonb), accessed via
  `jsonb_array_elements(wai_per_offer_scores) s` then `s->>'leg'`, `s->>'offer_id'`,
  `s->'signals'->>'adjacent_road_match'`, `s->>'confidence'`. NOT a top-level column.
- There is **no** top-level `pickup_error_m` / `dropoff_error_m` on this table (those live in
  the error-metric writer's own path — locate before querying).
- Key real columns: `wai_pudo_type`, `wai_confidence`, `wai_reason`, `wai_on_target_road`,
  `wai_current_road`, `wai_current_road_class`, `match_signal`, `matched_offer_id`,
  `arrest_duration_s`, `arrest_started_at`, `cluster_size`, `cluster_duration_s`,
  `peak_confidence`, `phase_reached`, `unmatched_reason`, `wai_per_offer_scores`,
  `lat`, `lng`, `speed_mph`, `gps_accuracy_m`, `gps_age_s`.
- Full schema: `\d app_private.pudo_decision_context` (pipe to a file — it wraps).
- DB access: `PGPASSFILE=~/.pgpass psql -h 10.128.0.2 -U postgres -d puddlejumper`

---

## 3. THE REVIEW — what to actually check

### 3.1 Inventory the drive (baseline)
Count today's legs: how many offers, how many arrests (`arrest_duration_s >= 5.0`), how many
PUDO observations fired (`matched_offer_id` non-null), how many `unmatched_reason` set. This is
the denominator for everything else.

### 3.2 PUDO ID rate (the launch-gate metric)
Of arrested legs with a live offer, what fraction observed (matched) vs missed? Target is
>95%. Compare to the ~86% implied by yesterday's 14%-miss finding — **the fix should move this
up.** This is the headline number for whether the fix worked in the wild.

### 3.3 Residential-adjacent recovery (did the fix do its job?)
Find today's intersection-class legs where `adjacent_road_match=1.0` (driver stopped on the
adjacent street). Under the OLD weights these scored ~0.29 and were lost. Under the new weights
they should clear 0.40. **Confirm: did any residential-adjacent pickup observe today that would
have been missed yesterday?** That's the live proof the fix works. (Pull `wai_per_offer_scores`,
look for intersection legs with adjacency firing and confidence now ≥0.40.)

### 3.4 The haircut watch (did the fix break anything?)
Find today's intersection legs with `adjacent_road_match=0` (non-adjacent matches). Confirm
none that SHOULD have observed landed below 0.40 due to the 11.1% haircut. Danger band:
legs whose composite is now in [0.40, 0.45) — they cleared but barely, and a hair more variance
would drop them. **Any non-adjacent intersection pickup that failed at ~0.40–0.45 today is the
haircut cost materializing — flag it, do not mistake it for a new bug.**

### 3.5 The red-screen recurrence (tabled, but verify once more on live data)
Andrew saw a "big red screen" on a dropoff this PM (he recalled it as offer ~"8029" but that id
returned 0 rows — the id was a guess; find the real dropoff by time window). This session's
checks showed: healthy Android offer screen at 15:00, clean heartbeat stream at 15:14 (no PUDO,
no offer — between rides), and **gcloud logs with zero errors/500s/exceptions** in the
19:00–20:15 UTC window. Conclusion carried in: **red screen = the tabled client-side
iPhone-web-monitor (`app.puddlejumper.io`) display glitch, NOT a data fault.** Data layer healthy.
- TO CLOSE THE LOOP: locate the actual dropoff that went red (by time window, not by the "8029"
  guess) and confirm it observed cleanly (matched, conf ≥0.40). If it observed → glitch confirmed
  again, leave tabled. If it did NOT observe → the red screen coincided with a real miss, which
  un-tables the finding (see FINDING §6). Only THEN does the red screen become a real thread, and
  the target is the **web dashboard repo, NOT `Puddle_Jumper` Android**.

### 3.6 Geocode-drift secondary (opportunistic)
Yesterday's §5 finding: geocode error scales with feature extent (freeway/farm-road dropoffs
drift km). If today's drive hit any freeway/long-road dropoffs, the error-metric data feeds the
GC spatial-gate (§4.6) future work. Capture, don't act.

---

## 4. METHOD DISCIPLINE (carried from this session — non-negotiable)

- **Recon before conclude.** Prove a mechanism with a query before naming a bug. This session
  killed SIX wrong theories by reading data instead of guessing. Do not name a fault today
  without the row that shows it.
- **Never hand-compute a verification target from eyeballed signals — pull from source.** This
  session shipped a CC brief with a wrong sanity target (0.434, real was 0.422) computed from a
  stale early-tick `cluster_duration=0.36` instead of the real best-tick 1.0. Caught by a STOP
  gate. When recomputing a composite, read the actual `wai_per_offer_scores` blob's best tick,
  not an eyeballed signal set.
- **A weight/threshold change's blast radius is everywhere the composite is COMPARED, not just
  where the weight is defined.** Yesterday's L-6 grep for `_CONFIDENCE_WEIGHTS` missed 3 of 4
  pinned tests because they keyed on `STRONG_MATCH_CONFIDENCE` and a TOML `expected_confidence_min`.
  If you touch a weight today, grep the threshold consumers too.
- **Best-tick = max recorded confidence per (offer,leg).** A leg spans many heartbeat ticks; the
  matcher's best shot is the peak. Use `peak_confidence` or max over the ticks, not an early tick.
- `STRONG_MATCH_CONFIDENCE = 0.70` is **test-only** — no production gate references it. Live
  gates are all 0.40 (`WAI_CONFIDENCE_THRESHOLD`, `COMMIT_NORMAL_FLOOR`). Don't treat 0.70 as
  behavioral.
- Paired-programming loop for any fix: Claude proposes → Gemini reviews → Andrew decides → CC
  implements → Andrew deploys. No code lands without that consensus. The venv for the test gate
  is at `~/puddlejumper-prod/venv/`; run pytest as `~/puddlejumper-prod/venv/bin/python3 -m pytest
  -x --tb=short` (the `activate` path tripped CC this session; the direct interpreter is robust).

---

## 5. OPEN HOUSEKEEPING (not part of the drive review, but pending)

- **INDEX.md is way out of date** and does not reference the 2026-06-03 finding (or likely many
  others). The document-driven-memory protocol is degraded until reconciled. This is its own
  task and deserves its own session — auditing `docs/` against the index entry-by-entry. Do NOT
  tack it onto the drive review. Draft entry for the 06-03 finding is ready in this session's
  notes if a quick add is wanted, but the real fix is a full reconciliation.
- The 2026-06-03 finding doc should be confirmed present at
  `docs/FINDING_residential_adjacent_pickup_below_floor_2026-06-03.md` with the SHIPPED status
  header (§4.6 as-shipped outcome). Verify the latest version (with §4.6) is the one on the VM.

---

## 6. ONE-LINE STATUS

Adjacency fix LIVE (rev `00642-fg7`, commit `9eebb45`), tests green, healthy. Today is its first
live drive. Review goal: confirm residential-adjacent recovery (§3.3) and watch the haircut
(§3.4). Red screen = tabled display glitch, confirm-and-leave (§3.5) unless the went-red dropoff
shows a real miss. INDEX.md reconciliation is separate and pending.
