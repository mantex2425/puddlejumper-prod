# P19 Deploy + Missed-PUDO Forensic — Evening Session 2026-05-20

**Date authored:** 2026-05-20 (evening, post-P19 deploy)
**Status:** Today's six missed PUDOs partition into THREE bug classes,
not one. P19 fixes one class. The other two converge on a foundational
data integrity issue: 255 ACCEPT offers across 38 days have
`actual_pickup_at IS NULL` and are keeping multiple downstream
predicates in degenerate states.
**Companion doc:** `SPRINT_HANDOFF_LOST_MODE_BIND_REGRESSION_2026-05-20.md`
(the morning sprint's findings — load both into next chat).

---

## TL;DR

Six PUDOs went unconfirmed in production on 2026-05-20. The forensic
partitions them across THREE independent failure modes:

| Miss | Time | `unmatched_reason` / `match_signal` | Bug class |
|---|---|---|---|
| 8076 DO | 03:58 | `cluster_unavailable` / `no_match` | Cluster engine blindness |
| 8080 PU | 05:07 | (cluster never formed) | Cluster engine blindness |
| 8094 PU window 1 | 07:06-07:07 | `cluster_unavailable` / `no_match` | Cluster engine blindness |
| 8094 PU window 2 | 07:08+ | (cluster formed n=7) / `lost_mode_no_candidate` | Queue/lost-mode |
| 8082 wrong-fire | 05:40 | (wrong location fired at 1.000) | Head 4 / Head 1 over-confidence |
| 8092 DO | 06:58-07:00 | (cluster formed n=11) / `lost_mode_ambiguous_observation` | Lost-mode ambiguity |
| 8080 DO | 05:24-05:26 | (cluster formed n=11) / no_match | `queue_actually_empty` |
| 8081 | (never queued) | (zero PDC rows) | Upstream ingestion |

**P19 (deployed today, revisions 00624/00625):** addresses the cluster
engine blindness class. Empirically validated via replay against 8076
DO and 8080 PU — both would have formed clusters under the new
stillness logic.

**P16/P18 (deployed today):** addresses the wrong-fire class for 8082.

**Lost-mode / queue divergence:** unresolved. Affects 8092 DO,
8094 PU window 2, and 8080 DO. THIS IS THE NEXT SPRINT.

**Upstream ingestion:** unresolved. Affects 8081 (zero PDC rows
anywhere today, no offer_history row queryable, never queued).

---

## The Foundational Finding: 255-row backlog

Live query at 17:00 Houston after P19 deploy:

```sql
SELECT COUNT(*) FROM app_private.offer_history oh
JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
WHERE dl.driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND oh.actual_pickup_at IS NULL
  AND oh.app_verdict = 'ACCEPT';

-- Returns: 255
```

These offers span **Apr 12 through May 20** — 38 days. The oldest in
the list is `oh_id 4302` from Apr 12 16:49. Every one is:
- `app_verdict = ACCEPT`
- `actual_pickup_at IS NULL`
- Predicate-alive per `LIVE_OFFER_PREDICATE_SQL`

This cannot represent reality. The driver has not had 255 simultaneously
active accepted rides for 38 days. **Something stopped writing
`actual_pickup_at` to `offer_history` at some point, OR something else
broke that lets ACCEPT offers stay forever-alive in the predicate.**

The morning handoff brief (lost-mode sprint) traces a smaller backlog
to the §XVIII Lost Mode commits from 2026-05-17. **The data extends
~30 days BEFORE those commits.** The §XVIII work may have made the
problem worse (zero `current_offer_id` binding = no
`actual_pickup_at` write path), but it did not CREATE the backlog.
A pre-existing condition got amplified.

### Architectural smell — divergent queue predicates

8080 DO's failure mode is particularly diagnostic. At 05:24-05:26:
- Cluster formed perfectly (n=11, 58+ seconds of stillness at
  29.6738/-95.5359)
- `matcher_candidates = {}` (empty)
- `unmatched_reason = queue_actually_empty`

But the offer 8080 is itself in the 255-row backlog of phantom-alive
offers. **So the matcher's queue accessor says "empty" while the
lost-mode detector's queue predicate says "lots of alive offers."**

This is two predicates reading two different sources:
- `_detect_lost_mode` uses `LIVE_OFFER_PREDICATE_SQL` (per the morning
  handoff doc, `driver_queue.py:183`)
- The matcher uses `matcher_candidates`, populated from
  `snap.offers` (the in-memory snapshot)

If they diverge, the system enters degenerate states: lost-mode
demotion fires (because LIVE_OFFER_PREDICATE_SQL sees offers),
matcher sees nothing (because snap.offers is empty), and the
planner's downstream actions are nonsensical.

### Cross-reference with morning handoff

The morning handoff focused on the §XVIII Lost Mode commits
suppressing `current_offer_id` binding via Observation-variant
demotion. THAT diagnosis is also correct — but it's a downstream
EFFECT of the 255-row backlog, not the root cause:

```
255-row backlog (root cause)
  ├──> LIVE_OFFER_PREDICATE_SQL returns many "alive" offers
  │      ├──> _detect_lost_mode latches True forever
  │      │      └──> dispatch.py demotes Fire* → *Observation
  │      │             └──> current_offer_id never bound
  │      │                    └──> Monitor alarm never fires (morning sprint)
  │      └──> "Lots of alive offers" confuses matcher when it
  │             can disambiguate (8092 DO ambiguous_observation,
  │             8094 PU lost_mode_no_candidate)
  └──> snap.offers diverges from LIVE_OFFER_PREDICATE_SQL
         └──> queue_actually_empty fires (8080 DO) even though
                LIVE_OFFER_PREDICATE_SQL sees 255+ alive offers
```

---

## What today's deploys accomplished

P19 + follow-up (revisions 00624-hbd, 00625-97d):
- `detect_cluster()` rewritten with strict stillness gating
  (speed < 0.5 mph, no spread gate, departure_grace 5s)
- `get_recent_clusters()` same logic with lower min_duration=5s
- `Cluster.started_at` field added for planner-side dedup anchor
- `app_private.pudo_decision_context.cluster_started_at` column
  added for forensic visibility
- `driver_heartbeat.py` writes the new column
- 31 cluster_detection tests pass; full suite 620 passed 1 skipped

P17/P18 (revision 00622-562):
- `routing.geofence_polygons` table populated with 3,245 Houston
  OSM polygons (airport / terminal / mall / hospital / etc.)
- Head 6 (geofence membership) added to `_match_poi_class` ensemble
- Head 1 (fuzzy name match) demoted to witness-only when geofence
  containment is present

P16 (revision 00621-cdq):
- Head 4 (POI type ensemble) demoted from confidence ensemble to
  witness-only

These five deploys reduce cluster-engine misses and wrong-fires
significantly. They do nothing for the 255-row backlog.

---

## Empirical replays — what tomorrow's drive should see

P19 retroactively validated against today's heartbeats:

```sql
-- 8076 DO replay (03:58:10 Houston)
-- Result: CLUSTER WOULD FORM, n=4, median (29.47323, -95.37181),
--         duration 17s

-- 8080 PU replay (05:07:30 Houston)
-- Result: CLUSTER WOULD FORM, n=4, median (29.60015, -95.37931),
--         duration 17s
```

Tomorrow's drive will exercise P19 in production. Expected:
- Short stops (10-25s) on rural / dense streets crystallize as
  clusters where they previously emitted `cluster_unavailable`
- Hobby airport / IAH airport dropoffs route through geofence
  membership (P18) at confidence 1.0 via IATA codes
- 8082-class wrong-fires no longer happen (Head 4/Head 1 demoted)

UNAFFECTED by today's work, expected to recur tomorrow:
- Lost-mode demotion of every `FirePickup` / `FireDropoff` to
  `*Observation` (Monitor alarm stays silent)
- `queue_actually_empty` when snap.offers diverges from
  LIVE_OFFER_PREDICATE_SQL
- Lost-mode ambiguity when multiple "alive" offers contend for the
  same stop location

---

## Next sprint scope (CONSOLIDATED)

Merge the morning handoff scope with this evening's findings. The
next sprint is not "the Monitor alarm doesn't fire" or "8080 DO
got evicted." Those are SYMPTOMS. The sprint is:

**Restore `actual_pickup_at` write integrity across the offer
lifecycle, drain the 255-row backlog, reconcile snap.offers with
LIVE_OFFER_PREDICATE_SQL.**

### Recon path

1. **Find the last day where the backlog was healthy.** Run:
   ```sql
   SELECT date_trunc('day', oh.created_at) AS day,
          COUNT(*) FILTER (WHERE oh.app_verdict='ACCEPT') AS accepts,
          COUNT(*) FILTER (WHERE oh.app_verdict='ACCEPT'
                            AND oh.actual_pickup_at IS NOT NULL) AS confirmed
   FROM app_private.offer_history oh
   JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
   WHERE dl.driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
     AND oh.created_at >= NOW() - INTERVAL '90 days'
   GROUP BY 1 ORDER BY 1 DESC;
   ```
   The drop-off (where `confirmed` collapses to near-zero) brackets
   the regression. Compare to git log of `driver_heartbeat.py` and
   the dispatcher's write paths.

2. **Diff the two queue accessors.** Find the code paths for
   `LIVE_OFFER_PREDICATE_SQL` and `snap.offers`. They should be
   equivalent. They're not. Identify why.

3. **Audit `actual_pickup_at` writers.** Grep for every code path
   that does `UPDATE offer_history SET actual_pickup_at = …`. There
   may be only one (`FirePickup` handler), in which case the §XVIII
   Observation demotion explains everything from 2026-05-17 forward.
   Earlier weeks need a different explanation.

4. **Drain decision.** Once root cause is fixed forward, decide what
   to do with the 255-row backlog. Options:
   - Hard-delete (lose forensic history)
   - Mark `actual_pickup_at = created_at + 1 hour` to expire predicate
   - Add a `predicate_force_expire_at` column and bulk-set it
   - Add explicit `closed_reason = 'historical_backlog_cleanup'` field

### What this sprint is NOT

- **Not a §XVIII unwind.** §XVIII Lost Mode is canonical; the bug
  is that the predicate it gates on is wrong (255 phantom-alive
  offers), not that lost-mode itself is wrong.
- **Not a frontend rewire.** Monitor alarm continues to work
  correctly once `current_offer_id` binding resumes.
- **Not a single-fix sprint.** Three forensic threads converge here:
  the predicate divergence, the writer path audit, and the backlog
  drain. Plan accordingly.

---

## Data captured this session

### Per-PUDO forensic shapes (today's six misses)

```
8076 DO @ 03:58 — cluster_unavailable, no_match
  Spread 51.16m at peak (slow approach + 23s stop merged)
  P19 replay: cluster forms (n=4, dur=17s)
  → CLUSTER ENGINE CLASS, fixed by P19

8080 PU @ 05:07 — no PDC clustering attempted (cluster never formed)
  Spread 38.1m at peak
  P19 replay: cluster forms (n=4, dur=17s)
  → CLUSTER ENGINE CLASS, fixed by P19

8080 DO @ 05:24-05:26 — cluster_actually_empty
  Cluster DID form (n=11, 58s, perfect crystallization at 29.6738,-95.5359)
  matcher_candidates = {} throughout
  unmatched_reason = queue_actually_empty for ~58s
  → QUEUE DIVERGENCE CLASS, unfixed

8082 wrong-fire @ 05:40 — fired wrongly at 1.000 confidence
  Head 4 binary type-match on broad POI category
  → WRONG-FIRE CLASS, fixed by P16+P18

8092 DO @ 06:58-07:00 — lost_mode_ambiguous_observation
  Cluster formed (n=3..11)
  Multiple morning-backlog offers contending for same area
  → LOST-MODE AMBIGUITY CLASS, unfixed

8094 PU @ 07:06-07:08 — TWO failure modes in one PUDO
  07:06-07:07: cluster_unavailable (cluster engine class)
  07:08+: cluster forms (n=3..7) but lost_mode_no_candidate
  → CLUSTER ENGINE (P19) + LOST-MODE (unfixed)

8081 — zero PDC rows anywhere today
  → INGESTION CLASS, unfixed
```

### Live state snapshot at end of session

```
Cloud Run revision: puddlejumper-api-00625-97d (P19 + follow-up deployed)
Pytest floor: 620 passed, 1 skipped
Branch: phase-2c-2-tad-exit-4tools, synced with origin
Last commit: 5236911 "P19 follow-up: tighten stillness threshold to <0.5 mph"
DB: 218,595+ PDC rows, 255-row backlog of phantom-alive ACCEPT offers
```

---

## Kickoff prompt for next chat

> Loaded: `EVENING_2026_05_20_BACKLOG_FOUNDATION.md` (this doc).
> Companion: `SPRINT_HANDOFF_LOST_MODE_BIND_REGRESSION_2026-05-20.md`
> Standard load: `CANONICAL_RULES.md` (§0, §XIV.I, §XV, §XVIII),
> `SESSION_PROTOCOL.md`, `INDEX.md`.
>
> Mission: the Monitor alarm regression, the 8080 DO
> queue_actually_empty failure, and the 8092 DO
> lost_mode_ambiguous_observation all converge on a single root
> cause: 255 ACCEPT offers across 38 days have
> `actual_pickup_at IS NULL`, keeping LIVE_OFFER_PREDICATE_SQL in a
> degenerate "everything is alive" state while snap.offers
> independently presents an empty queue. The §XVIII Lost Mode
> demotion suppresses the write path for the alarm, but did not
> CREATE the backlog (which predates §XVIII by 30+ days).
>
> Per session protocol: recon first. Start with the daily backlog
> trend query (recon step 1 in this doc's "Next sprint scope")
> to bracket when `actual_pickup_at` writes broke. Propose the
> recon sequence for ratification before running.
