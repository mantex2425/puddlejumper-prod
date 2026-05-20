# RFC P19 — Cluster Engine Stillness Refactor

**Status:** Drafting (Andrew + Gemini + Claude 2026-05-20)
**Sequence:** Follows P18 (geofence membership). Foundational refactor of cluster_detection.py.

---

## 1. Motivation

Empirical findings from production drive 2026-05-20:

- **8076 dropoff (Manvel, 23s stop):** real-world spread bug. Driver decelerated through approach corridor (~40m at 6-9 mph) then fully stopped. `detect_cluster` pulled the slow-approach samples into the cluster (because their speed < 10 mph kept `breaks_before = 0`), computed median between approach and stop, and rejected the cluster on spread > 25m. `cluster_unavailable` logged. Geofence never evaluated.
- **8080 pickup (S Sam Houston Pkwy E, 33s stop):** same pattern. Verified empirically: `detect_cluster` at 05:07:30 returned n=7, max_spread_m=38.1, rejected.
- **8094 pickup (Hobby Main Terminal):** likely same pattern (approach corridor in dense urban environment).

Two of six missed PUDOs today are explained by this single architectural flaw. The geofence stack (P17/P18) is downstream of cluster formation. If `cluster_unavailable`, geofence never evaluates.

## 2. Root Cause

Current `cluster_detection.detect_cluster()` uses a **rolling 60-second time window** and includes any heartbeat where `speed_mph < 10.0` in the cluster computation. This conflates two physically distinct events:

1. The **approach corridor** (3-9 mph deceleration over 20-100m)
2. The **actual stop** (speed = 0, vehicle stationary at one point)

The median lat/lng is pulled toward the centroid of (corridor + stop). Spread is then computed against that pulled median, which inflates spread beyond the 25m threshold even though the stop itself has spread close to 0m.

## 3. Empirical Calibration

Examined every speed=0 reading across today's drive (~50 stops). Every observed stop registered as **exactly 0.00 mph** in the heartbeat_log. No GPS drift readings at 0.3, 0.5, or 0.7 mph. The Android app's GPS pipeline does the low-pass filtering. **Stillness = speed_mph = 0.0** is a reliable hard threshold.

## 4. Refactor

### 4.1 New `detect_cluster()` contract

Returns the most recent stillness run meeting:
- `speed_mph = 0.0` across all samples
- duration >= `min_duration_s` (default 10)
- n samples >= `min_samples` (default 3)
- run ended within `departure_grace_s` of now (default 5; covers the pull-away race window)

Else None. No spatial spread gate. Contiguous speed=0 samples are at a single physical location modulo GPS noise (typically <5m).

### 4.2 New `detect_recent_clusters()`

Returns ALL distinct stillness runs in the `lookback_s` window (default 300), sorted by recency. Use case: multi-stop PUDO disambiguation (apartment-complex pickup with leasing-office stop, gate stop, building stop — matcher reasons about all three).

### 4.3 SQL strategy

Both functions use the same gaps-and-islands pattern. Stillness runs are contiguous speed=0 heartbeats separated by speed>0 gaps. The same SQL backs both functions; `detect_cluster` selects with `ORDER BY ended_at DESC LIMIT 1` and filters by departure_grace_s; `detect_recent_clusters` returns the full list.

The current implementation uses `percentile_cont(0.5) WITHIN GROUP` inline. The gaps-and-islands version requires either:
- Two-pass query (compute run_id and median in one CTE; compute spread against the median in a second CTE), OR
- Post-fetch Python computation of spread_m

Spread is informational only post-refactor (no gating), so the SQL can return median + duration + n only, with spread computed in Python from raw points if needed for forensic.

### 4.4 What is removed

- `max_speed_mph = 10.0` parameter (no longer needed — stillness = speed=0)
- `max_spread_m = 25.0` parameter (no longer needed — stillness runs are tight by construction)
- `routing.known_stops_config.min_cluster_duration_s` table reference (duration is now a function parameter)

The `Cluster` dataclass keeps `spread_m` field for backward compatibility but it becomes informational only.

## 5. Behavior Changes

### Stops that NOW form clusters but previously didn't:
- Short stops (10-15s) following a slow approach corridor: 8076 DO confirmed, 8094 PU likely
- Crawl-then-stop patterns: 8080 PU confirmed

### Stops that previously formed clusters but now might not:
- Stops where speed never hit exactly 0.0 (creep parking, illegal idle). Empirically we don't see these in driver data today — theoretical regression only.

### New capability: multi-stop reasoning
- Apartment complex stops (leasing office, gate, building) become three distinct clusters
- Airport curb hunting (stop-creep-stop while looking for PAX) becomes multiple clusters
- The matcher decides which is the PUDO based on duration, geofence containment, contest labels

## 6. Departure Grace Window

The 5-second `departure_grace_s` parameter handles the race condition where a driver pulls away from a curb between heartbeats. Without it, `detect_cluster` would return None at the first heartbeat showing speed > 0, even though the stop ended <1s earlier. With it, the planner gets one more heartbeat tick to fire if conditions are met.

Trade-off accepted: a "ghost cluster" can persist for 5 seconds after motion resumes. This is benign — by the time another heartbeat tick fires, the run is past the grace window and clears.

## 7. Test Plan

### Unit tests (new file `tests/test_cluster_detection_stillness.py`):
1. `test_detects_pure_stop` — 4 consecutive speed=0 samples spanning 15s → returns Cluster
2. `test_rejects_short_stop` — 2 speed=0 samples spanning 7s → returns None
3. `test_ignores_slow_approach` — corridor of speed=6 then 4 speed=0 samples → cluster median is at the stop, not the corridor
4. `test_departure_grace_active_when_recent` — last sample 3s ago at speed>0, prior 4 samples at speed=0 → returns Cluster
5. `test_departure_grace_expires` — last sample 10s ago at speed=0, then 8s at speed>0 → returns None
6. `test_recent_clusters_returns_multiple` — three distinct stillness runs in the lookback → returns 3-element list
7. `test_recent_clusters_excludes_brief_runs` — one 8s run and one 20s run → returns 1-element list (just the 20s)

### Replay tests (using today's heartbeat_log data):
8. `test_replay_8076_dropoff_clusters_at_03_58_04` — historical query against (29.4732, -95.3718) window → returns Cluster, NOT None
9. `test_replay_8080_pickup_clusters_at_05_07_30` — historical query → returns Cluster, NOT None
10. `test_replay_high_speed_driving_returns_none` — historical query during the 04:05 highway segment → returns None

Pytest floor: 613 -> 623.

## 8. Migration Strategy

`detect_cluster` keeps its name and return type (Optional[Cluster]). Call sites in `driver_heartbeat.py` need no changes. Only the implementation rotates.

`detect_recent_clusters` is a new function added to `cluster_detection.py`. No call sites yet — initial deploy provides the capability; subsequent work in `where_am_i.py` may opt into using it for apartment-complex / multi-stop offers.

## 9. Risks

1. **Behavior change at every PUDO.** Tomorrow's drive will exercise the new engine. Validation drive should include intentional approach-then-stop patterns to confirm the fix.
2. **Lower duration floor (10s vs current 15s).** May admit longer-than-expected red-light stops. Mitigated by downstream geofence/semantic matchers naturally rejecting non-venue clusters.
3. **Departure grace introduces 5s of "ghost" cluster state.** Could in principle cause double-fires if a cluster fires at end-of-stop and then again 4s later as departure_grace keeps it visible. Mitigated by the planner's existing `current_offer_id` state tracking which prevents re-firing the same offer.

## 10. Out of Scope

- Red-light DB / RR-crossing DB — natural noise suppression via downstream matchers handles this
- TTL decay state machine — heartbeat_log is the source of truth; no in-memory cache needed
- Refactoring `known_stops_config` table — the row stays, the lookup goes (config row becomes orphaned but harmless)


---

## 11. Revision 2 — Gemini ratification 2026-05-20

Three updates per Gemini review:

### 11.1 Cluster dataclass gains `started_at`

The departure_grace window creates the risk that a cluster object persists across two heartbeat ticks: the tick that fires the PUDO, and the next tick where the same cluster is still "visible" due to grace. The planner avoids double-fire by comparing `cluster.started_at` against the started_at of the last cluster it processed.

Schema:

```python
@dataclass(frozen=True)
class Cluster:
    n: int
    median_lat: float
    median_lng: float
    spread_m: float      # informational only
    duration_s: float
    started_at: datetime # NEW: dedup anchor
    latest: datetime     # existing
```

The planner records last_processed_cluster_started_at in its current_offer_id state register. If the next tick yields a cluster with the same started_at, the fire path short-circuits.

### 11.2 Differentiated duration floors

- detect_cluster(): min_duration_s = 10 (unchanged). The active fire trigger requires a real stop, not a red-light pause.
- detect_recent_clusters(): min_duration_s = 5 (lower). Provides historical visibility into micro-stillness islands so the matcher can reason about stop-creep-stop patterns at airport curbs (8s plus creep plus 9s should be observable as two adjacent clusters).

### 11.3 Single-pass SQL confirmed

Dropping spread_m gating (per RFC section 4.4) eliminates the two-pass requirement. The gaps-and-islands query uses percentile_cont(0.5) WITHIN GROUP (ORDER BY lat) with GROUP BY run_id in a single pass. PostgreSQL ordered-set aggregates handle this natively.

### 11.4 Forensic schema addition

Add cluster_started_at timestamptz column to app_private.pudo_decision_context. The planner writes the cluster's started_at on every row. Post-deploy queries can:

- Detect double-fires: same driver_id and cluster_started_at appearing across multiple rows with different planner_action values
- Measure departure_grace saves: rows where (created_at - cluster_started_at) > duration_s
- Multi-cluster correlation: same current_offer_id across two rows with different cluster_started_at values

Migration is additive (nullable column), backward compatible.

Pytest floor moves: 613 to 624 (added test for started_at dedup behavior).
