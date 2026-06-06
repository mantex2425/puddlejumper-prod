# Feature Proposal — DSI (Drive Score Index)

**To:** Andrew (decisions), Gemini (review), then CC (implement)
**From:** Andrew + Grok + Claude
**Date:** 2026-06-02
**Status:** Strong direction. Schema + backfill ready to proceed. THREE items
gated before DSI controls live verdicts (§4).

---

## 1. What DSI Is

A single composite score combining dollars-per-hour and dollars-per-mile
(cost-adjusted) into one number, replacing the current dual-metric decision logic
with one consistent value both the driver and the decision engine can use.

**Standard DSI (stored in database):**
```
DSI = effective_hourly_rate + DSI_MILE_WEIGHT × (dollars_per_mile − IRS_RATE_PER_MILE)
```
- `IRS_RATE_PER_MILE` = **0.725** (2026 IRS business mileage rate) — a NAMED
  constant, defined once, read by both the ingest writer and the backfill. Never
  hardcoded inline (canonical-predicates discipline).
- `DSI_MILE_WEIGHT` = **12** (initial value — to be validated, §4.1).
  **This `12` is a WEIGHTING MULTIPLIER, NOT a months-amortization.** (It
  coincidentally equals an earlier "×12 months" phone discussion; they are
  unrelated. Stated explicitly so it is never "corrected" to a months meaning.)
- The standard DSI (using 0.725) is what is STORED — one consistent dataset across
  all drivers.

**Personal DSI:** computed at decision time only, using the driver's own
`cost_per_mile` from user settings (**already stored — dependency only, no new
storage**), default 0.725. Personal DSI is NOT stored; runtime-only.

---

## 2. Schema Changes

One new column per table (matches the "one composite number" decision):
- `app_private.offer_history.dsi_standard` (double precision, nullable)
- `app_private.community_offers.dsi_standard` (double precision, nullable)

No other new columns. No new storage for personal cost-per-mile (already in user
settings).

Inputs already exist on `offer_history`: `effective_hourly_rate`,
`dollars_per_mile` (or derived from `fare`/`trip_miles`/`trip_minutes` — confirm
canonical source at build). Uber displays BOTH $/mi and $/hr on the card, so no
reverse-engineering needed.

**`dsi_standard` is stored PER-ROW, never pre-aggregated.** The historical
comparison runs through the live IDW Price Radar (§4.3), which interpolates from
individual `1/d²`-weighted points — a bucket-summary DSI would be unusable to it.
Per-row is a hard requirement of the IDW comparator, not a preference.

---

## 3. Where DSI Will Be Used

1. **Historical analysis (web portal):** tables + maps of average `dsi_standard`
   by time-of-day / day-of-week / zone. The primary "when should I drive?" tool
   (and the dataset for the article).
2. **Real-time decision engine:** compute the driver's PERSONAL DSI for the
   incoming offer; compare against historical standard DSI via the existing IDW
   Price Radar (extended to carry DSI — §4.3); drive the Accept/Decline decision
   and frog color.
3. **Visual feedback:** frog icon temporarily replaced by the large centered DSI
   number; green bg = good, red bg = poor; same timeout as the current glow.
   **See §5 red-screen collision.**

---

## 4. Gated Items — Must Validate Before DSI Controls Verdicts

Schema + backfill may proceed; DSI MUST NOT drive Accept/Decline until these
three are calibrated against real (backfilled + labeled) data.

### 4.1 `DSI_MILE_WEIGHT` (=12) is unvalidated — with proof it matters
Magnitude check on yesterday's known-decline trap (offer 8858: ~$0.60/mi,
~$23/hr): `23 + 12 × (0.60 − 0.725) = 21.5`. A ride Andrew correctly DECLINED
scores **21.5**, which would PASS a naive $20 floor. So either the weight
under-penalizes mileage or the threshold is wrong — proven, not hypothetical. Tune
the weight so the score cleanly separates known-good from known-bad rides in the
backfilled history.

### 4.2 Accept threshold is unvalidated
"$20" was a conversational starting point, not a calibrated cutoff for THIS
formula. Derive the final rule (personal DSI ≥ historical DSI + X? ≥ Y
percentile? a delta/ratio floor?) from the backfilled distribution + labeled
rides, not by assertion.

### 4.3 Comparator logic — anchors on the LIVE IDW Price Radar
The live price-lookup is the IDW Price Radar (`get_price_radar()`), interpolating
from nearby confirmed points by `1/d²` with recency decay. DSI rides that same
path:
- Compute the offer's personal DSI; compare against an IDW-interpolated historical
  DSI at the pickup coords — `get_price_radar()` extended to carry a `dsi_standard`
  channel alongside `idw_hourly`/`idw_mileage`, same weighting math (DSI is just
  another per-offer scalar; no new spatial machinery).
- **Confirm the radar's actual signature / return columns against the LIVE code at
  build — do NOT assume from memory.** (The radar's promotion from shadow mode to
  live post-dates Claude's stored notes; the tree is the authority.)
- Define the exact comparison rule (§4.2) and reuse the radar's existing
  `confidence_tier` / `point_count` for sparse-signal fallback — do not invent a
  parallel fallback.

---

## 5. Important Notes

- **Ride types:** UberX / Comfort / Priority blended into one DSI for V1. Revisit
  if ride-type skew shows up in calibration.
- **Red-screen collision:** the unexplained red-screen "alarm" on dropoff confirms
  (8851, 8857 on 06-02; **9007 on 06-03 — third occurrence, first on the
  291-fixed tree**) is STILL un-diagnosed. DSI also paints the background red for a
  poor score. Two red signals would be indistinguishable in the field. **Resolve
  the sentinel alarm before DSI's red ships, OR make DSI's treatment visually
  distinct.** Do not stack a second red onto an unexplained one.
- **Backfill:** recompute `dsi_standard` across historical `offer_history` and
  `community_offers` after the schema + named-constant land. One-time, idempotent
  (deterministic recompute), quarantined as a separate pass — NOT bundled with the
  ingest-writer change. The backfill is also what §4.1/4.2/4.3 calibrate against.

---

## 6. Sequencing

1. Land named constants (`IRS_RATE_PER_MILE`, `DSI_MILE_WEIGHT`) + the two columns
   + the ingest writer (store standard DSI on every new offer). No verdict wiring.
2. Backfill historical rows (per-row `dsi_standard`).
3. **Calibrate** weight + threshold + comparator (§4) against backfill + labeled
   rides. Analysis, not code.
4. Only then wire DSI into the verdict + ship the visual (after §5 resolved).

---

## 7. Open Questions for Gemini

1. Column name `dsi_standard` vs version-encoded `dsi_v1` (so a future
   weight/IRS change is distinguishable in history)?
2. Engine compares PERSONAL offer DSI against STANDARD historical DSI (current
   spec, one dataset), or re-derives historical DSI at the driver's cost-per-mile
   on read (personal-vs-personal)? Flagging the asymmetry.
3. Does every `community_offers` row carry the $/mi and $/hr inputs needed to
   compute `dsi_standard`, or are some too sparse (→ leave NULL)?

---

## 8. Phase-1 Validation — PASSED (pre-build, zero code)

Run 2026-06-02 against 8 months of `offer_history` (`is_validated = true`,
non-NULL rate/mile). DSI computed inline (standard 0.725, weight 12) — no schema
change, no column, no deploy. Three reads, all confirming the feature is worth
building BEFORE writing any code.

### 8.1 Variation across time/day — PASS
DSI ranges **15.1 → 31.2** across day×time buckets (~2x spread; not clustered).
Notable, contrarian signal:
- **Saturday midday (10-13 = 31.2, 14-17 = 26.9)** is the week's best window.
- **Weekend nights are a trap:** Sat 18-21 = 16.7, Fri 18-21 = 16.9 — among the
  worst, despite conventional "drive weekend nights" wisdom.
- Weekday afternoons (14-17) run strong across multiple days.
Caveat: peak cells are small-n (Sat 10-13 is n=18) — trust the n≥100 buckets
(~19) as the stable baseline; treat peaks as directional until more data.

### 8.2 DSI adds information beyond $/hr — PASS
Split by IRS line: the mileage term moves DSI **−3.6 (mi<0.725)** to **+4.4
(mi≥0.725)** — an 8-pt swing. DSI is NOT collinear with $/hr; the mileage
component does real separating work. This answers §4.1's "is 12 too timid"
concern: the weight is surfacing real signal, not noise.

### 8.3 The hidden-cost thesis, quantified — PASS
**1456 of 2567 recorded offers (57%) fall BELOW the 0.725 mile line** — their
decent-looking hourly ($15.5 avg) masks a mileage cost that drags true DSI to
12.0. DSI is the one number that catches the majority-case sneaky-bad ride.

### 8.4 What Phase 1 does NOT yet prove (still gated, §4)
- Uses standard 0.725, not the driver's real personal cost (the asserted "0.45"
  is UNVERIFIED — confirm from user settings before any accept/decline backtest).
- Survivorship-limited: `offer_history` = recorded offers, weighted to engaged
  ones. Relative variation is valid; absolute thresholds and the
  declined-offer blind spot remain for Phase 2.
- Threshold (§4.2) and comparator rule (§4.3) still uncalibrated.

**Verdict:** DSI varies, separates from $/hr, and quantifies a real hidden cost
across a majority of offers. Feature validated as worth building. Schema +
backfill cleared to proceed; live-verdict wiring still gated on §4.

---

## 9. Conceptual Foundation (front-of-mind — the spine of the whole case)

DSI's value rests on TWO distinct principles, braided together. Keeping them
separate is what makes the case un-attackable; conflating them is how the argument
gets lost.

**Principle 1 — Market-relativity.** "Lousy" is meaningless in absolute terms. A
$0.50/mi offer is not bad if the going rate for THIS H3 + time + day is $0.50/mi —
it's simply market. The engine's job is "is this offer at or above the local
market rate," not "is this offer good in the abstract." The IDW radar already
provides this baseline. **Note: the CURRENT two-surface radar is ALSO
market-relative** (it compares offer-$/hr vs market-$/hr and offer-$/mi vs
market-$/mi). So market-relativity is NOT DSI's unique advantage — do not claim
"DSI is relative and the old way is absolute." That claim is false and a sharp
reviewer will break it.

**Principle 2 — Tradeoff-awareness (THIS is DSI's actual edge).** The two-surface
system combines its two relative comparisons with a brittle **AND gate**: a ride
must beat market on hourly AND on mileage. A ride far above market hourly but below
market mileage FAILS the AND and is declined — even though its blend is excellent.
DSI collapses to one market-relative number where strength on one axis compensates
weakness on the other. **A single market-DSI comparison structurally cannot
produce the two-floor leak.** This is the defect proven by the 20 Freestyle rows
(§ engine-disagreement finding): high-$/hr/low-$/mi and high-$/mi/low-$/hr rides
declined by single-axis floors.

**The bulletproof one-line claim:** *DSI is market-relative AND tradeoff-aware in a
single number* — where today's engine compares each axis relative-to-market but
combines them with an AND that ignores the tradeoff.

### 9.1 Personal-vs-standard cost basis — a DECISION, not a confound
Comparing PERSONAL DSI (driver's cost/mile, e.g. 0.45) against MARKET DSI (standard
0.725) introduces a fixed offset: personal DSI is higher by `DSI_MILE_WEIGHT ×
(0.725 − personal_cost)` on every ride (≈ +3.3 pts at 0.45). This is INTENDED, not
a bug: a driver with a cheaper car genuinely finds more rides worthwhile than the
average driver, and the offset encodes exactly that. But it must be a conscious
design choice, not a buried artifact — the engine is asking "is this ride worth it
*for me*," measured against "what the market generally pays." (This is open
question #2 for Gemini — resolve explicitly.)

### 9.2 DSI formalizes an override the operator already performs (behavioral validation)
Andrew reports that when an offer has high $/hr but failing $/mi (or vice versa),
he **frequently overrides the engine's DECLINE and drives it anyway** — i.e. he is
computing the DSI tradeoff by gut. If true, the 20 Freestyle disagreement rows are
not 20 engine errors; they are 20 cases where the engine and the operator's
judgment diverged and the operator trusted his judgment. DSI would AUTOMATE and
OBJECTIFY that override — which is why the system would "feel more sensible": it
makes the engine agree with the judgment the best-informed user already applies.

**Success metric, reframed (more honest than "earns more"):** DSI's verdicts should
MATCH Andrew's actual overrides. **Caveat — UNCONFIRMED:** the database does not
record what was truly driven (only engine verdict + PUDO detection, not Uber's
completed-ride truth). "I often drive the declined ride" is operator recollection,
a strong hypothesis, NOT data-confirmed — same class as the sentinel/radar memory
claims the tree corrected today. **Killer validation when the Uber export lands:**
of the 20 Freestyle high-DSI declines, how many appear as COMPLETED rides in Uber's
data? Most → override pattern confirmed, DSI proven to formalize it. Few → revise
the story.

---

## 10. Foundation Gates — both PASSED (pre-build, read-only)

### 10.1 Gate 2a — input definition (PASS)
`effective_hourly_rate` and `dollars_per_mile` are NOT computed by PuddleJumper.
`decisions/router.py:561-562` takes them directly from the POST payload
(`p.get("hourlyRate")`, `p.get("dollarsPerMile")`) — i.e. they are **Uber's
displayed card values, captured verbatim**. Consequences:
- All 8 months of `offer_history` use one consistent definition → historical
  analysis (variation table, 20-row leak, beat-market proof) is internally sound.
- DSI MUST be specified as consuming **Uber's displayed rates**, NOT a
  PuddleJumper-derived `fare/time` recompute. A future "improvement" that
  recomputes $/hr from components would diverge from both Uber's number AND the
  historical column — forbid it.
- **Open one-hop check (Android repo, not blocking):** confirm the Android client
  sends Uber's raw card values and doesn't itself recompute them upstream.
- Note: today's voice experiment derived $/hr from `fare ÷ (pickup+trip time)`,
  which may differ from Uber's `hourlyRate` basis. The STORED column and the
  product use Uber's value; the voice mismatch was a manual-derivation artifact,
  not a system issue.

### 10.2 Gate 2b — is the 2-D→1-D collapse lossy? (PASS, with UI consequence)
DSI-band axis-spread check across `community_offers` (validity-bounded $/hr<150,
$/mi<10): hourly min/max climbs **monotonically** band over band (band 3:
$12-23/hr → band 7: $21-34 → band 11: $29-75). DSI is a faithful ORDERING signal —
higher DSI genuinely = higher-value territory. Confirmed safe for the readout.
- **Controlled overlap at the threshold:** the mid bands (esp. band 5, DSI~20-25,
  n=352 — the decision zone) span $13-31/hr and $0.00-1.43/mi. A DSI-22 can be
  hourly-strong/mileage-weak OR the reverse. This is the tradeoff-compensation
  working AS DESIGNED, not a defect — but it's most ambiguous exactly at the
  accept/decline line.
- **UI consequence — RESOLVED by the minimal interface (§11):** the driver only
  ever sees one number + color, so the threshold ambiguity is never surfaced and
  never needs to be. A driver at speed cannot process a two-axis tradeoff anyway;
  collapsing to one number is the only consumable signal in-context. The personal
  cost-per-mile adjustment re-sorts the collided rides by the driver's own
  economics BEFORE the number reaches the screen. No axis display anywhere.

**Both gates pass → schema + writer (step 1) is justified on verified ground.**

---

## 11. Interface — deliberately minimal

- **Visual (always on):** ONE DSI number + a color under the frog. Red < 20,
  yellow 20-25, green > 25 (thresholds PROVISIONAL — validated/tuned in shadow
  mode against real distribution). No axis breakdown, no verdict screen — the
  single number+color IS the interface.
- **Vocal (optional, driver toggle, OFF by default):** may speak the number /
  verdict. Assumed inaudible or ignored in busy conditions (Saturday night), so
  NEVER load-bearing. The visual is the real channel.
- **Live vs historical toggle:** driver chooses the lens — live local market DSI
  (all recent offers, sparse with one driver today) vs historical aggregate (8
  months, dense). Default historical until community volume makes live stable.
- **Market signal = ALL offers, not just confirmed.** The readout/market-DSI is
  built from every offer in `community_offers` regardless of accept/decline —
  declined offers are valid market signal (Uber priced them; the rate is real).
  Do NOT filter to `nail_it` only.
- **Lookup via the IDW radar at exact coordinates, NOT H3-cell snap** — avoids
  boundary jumps as the driver moves; smooth readout.

### 11.1 The single number IS the product thesis
The value is not the formula (copyable in minutes) — it is the LIVE VISIBILITY of
one honest, market-relative, tradeoff-aware number that answers the question a
driver couldn't previously verbalize: *"Is where I am right now any good?"* Red =
move, green = stay. That qualitative shift (invisible multi-axis judgment →
visible single signal) is the feature. The moat is the data + community that
populate it, not the arithmetic.

---

## 12. Build arc (sequential — each step gates the next)

1. **Schema + named constants + ingest writer** — `IRS_RATE_PER_MILE`,
   `DSI_MILE_WEIGHT`, `dsi_standard` columns, store on every new offer. No verdict
   wiring, no UI. (FOUNDATIONAL — all below depend on it.)
2. **Backfill** `dsi_standard` across `offer_history` + `community_offers`
   (per-row, deterministic, idempotent, quarantined).
3. Then, in parallel:
   - **Shadow-mode readout** — number + color under frog (radar lookup, all
     offers, live/historical toggle). Immediately useful for repositioning.
   - **Beat-the-market proof** — avg DSI of accepted rides vs avg DSI of ALL
     offers, per market. No Uber export needed; the data is already in-DB.
   - **Multiplier optimization** — sweep `DSI_MILE_WEIGHT` candidates, find the
     value maximizing the accepted-vs-market spread.
4. **Weekly cron auto-recalibration** (GATED, phase 3+) — re-optimize the weight
   on a schedule WITH GUARDRAILS: bounded change (±0.5/wk, hard floor/ceiling),
   minimum sample before adjusting, full audit log (old/new/justification),
   cross-driver objective (not single-driver overfit), and only after DSI drives
   live verdicts. A self-modifying production weight needs rails.

### Marketing frame (for the business plan, not the build)
"Secret formula" (Coca-Cola) positioning: lean in, don't hide. Headline claim,
once proven: **"Uber drivers using PuddleJumper's DSI beat the market by X%"** —
provable from `community_offers` alone (accepted DSI vs all-offer DSI), updates
live as the community grows, and competitors can't replicate it (the data + the
self-calibrating weight, not the math).

---

## 13. Weight Calibration — CONCLUSIVE NEGATIVE RESULT (the weight is not data-derivable)

Tested whether `DSI_MILE_WEIGHT` can be optimized from in-database offer data.
Two independent objectives, both read-only sweeps over weights 8-16:

**Objective A — maximize accepted-vs-market spread** (accepted-offer DSI minus
all-offer DSI):
- Spread climbs MONOTONICALLY 9.04 → 11.21 across weights 8→16. No peak. Accepted
  stddev climbs in lockstep (12.9 → 15.8).

**Objective B — maximize ACCEPT-vs-DECLINE separation** (Freestyle only, the
pure-economic subset):
- Separation climbs MONOTONICALLY 16.6 → 18.3 across weights 10→14. No peak. Both
  group stddevs rise in lockstep (accept 12.5→13.8, decline 9.2→10.0).

**Diagnosis:** both objectives fail identically — a higher weight mechanically
amplifies whatever axis-gap already exists between the two populations, inflating
the "score" and the scatter together, with no interior optimum. Naive optimization
of either would run the weight to infinity. **With only $/hr and $/mi as inputs and
no OUTCOME label, the weight has no data-derived optimum.** This is a property of
the data, not a tuning problem to solve harder.

### 13.1 Consequences (these SUPERSEDE earlier sections)
1. **`DSI_MILE_WEIGHT = 12` is a deliberate PRODUCT PARAMETER (a judgment call),
   NOT a data-derived constant.** Two independent objectives confirm the offer data
   cannot pick it. It is chosen (it makes the mileage term meaningfully separate —
   the ±4 swing in §8.2), like a threshold, not discovered. Treat as settled, not
   pending.
2. **The weekly optimization cron (§12 step 4) is STRUCK, not deferred.** It was
   premised on maximizing spread/separation — both proven unsound. Do NOT build a
   weight-tuning cron on any rate-only objective. If weight-tuning is ever built, it
   must be a PREDICTIVE model against real completed-ride OUTCOMES (Uber export):
   "which weight best predicts the value of rides actually driven" — an objective
   that CAN be wrong in both directions and therefore has a real optimum. That is
   the ONLY honest path to an optimized weight.
3. **Optimizing against `app_verdict` (Objective B) is the WRONG TARGET regardless**
   — it would tune DSI to AGREE with the current engine, including the 20 Freestyle
   leak rows where the engine is provably wrong. Mimicking a flawed engine is the
   opposite of the goal.

### 13.2 What the data DID settle — the THRESHOLD (not the weight)
The Freestyle diagnostic showed the engine's decision line sits at ~DSI 30 (accept
mean) vs ~13 (decline mean). The provisional accept threshold (~22-25) sits in the
natural trough between these populations — so the THRESHOLD is data-SUGGESTED
(~22-25) even though the WEIGHT is not. Net: **weight = judgment (12); threshold =
data-suggested (~22-25), to be finalized in shadow mode.**

### 13.3 The epistemic boundary (the spine of DSI's honesty)
DSI's claims separate cleanly by what the data can support:
- **CAN validate from offers:** DSI varies (§8.1), separates from $/hr (§8.2),
  orders rides monotonically (§10.2), and beats market by a weight-independent
  +9-11 DSI on engine-accepted offers (§13 Obj A — real, and weight-independent).
- **CAN suggest from offers:** the threshold (~22-25).
- **CANNOT derive from offers:** the weight (this section) and any revenue/outcome
  claim — both need the OUTCOME label (what was actually driven + earned), which
  lives in Uber's export, NOT in PuddleJumper's DB (which holds decisions, not
  outcomes).

**This is why the Uber export is on the critical path** — not for the revenue proof
alone, but as the only source of the outcome label that makes BOTH honest weight
optimization AND the revenue backtest possible.

---

## 14. Backfill complete + data-quality finding (radar read-filter spec)

STEP 1 (schema+writer) deployed (rev 00644-w4s); STEP 2 backfill committed
(0884865) — all 4093 historical rows carry `dsi_v1` (writer-parity proven,
NULL-strict, idempotent, single-txn). offer_history avg 20.0, community_offers
20.7 — matches the inline-computed expectation.

### 14.1 The negative-DSI population splits into TWO kinds — treat them OPPOSITELY
Counts across the backfill: offer_history 288 negative (10.9%), community_offers
123 (8.5%). Decomposed:
- **JUNK (~194 oh / ~79 co):** `effective_hourly_rate` or `dollars_per_mile`
  recorded as **0 or NEGATIVE**. A negative $/mi is physically impossible →
  OCR/capture errors, not offers. These are the toxic IDW points. FILTER at
  radar-read.
- **LEGITIMATELY NEGATIVE DSI (~94 oh):** positive-but-below-breakeven rates (real
  offers, real money-losers vs the IRS line). **These are NOT junk — they are the
  core signal DSI exists to surface** (the §8.3 hidden-cost thesis). KEEP them.

### 14.2 Radar read-filter rule (for the Android UI / radar step — NOT a backfill change)
The stored column stays writer-faithful (junk included) so the historical ledger
matches the live writer exactly. The RADAR READ PATH applies a validity filter so
junk never reaches IDW interpolation:
- **CORRECT filter (input sanity):** exclude rows where `effective_hourly_rate <= 0
  OR dollars_per_mile <= 0` (impossible = capture error). Also apply the existing
  project bounds (`< 150` / `< 10`, per `timelapse.py`).
- **WRONG filter (do NOT use):** `dsi_v1 > 0` — this would discard the ~94
  legitimately-negative real offers, deleting the exact money-loser signal the
  product is built to expose. Filter on INPUT validity, never on DSI sign.
This is the read-time analog of the writer's NULL-strictness: ledger faithful,
consumed surface clean.

### 14.3 Optional separate pass (out of scope, no clock)
The ~273 junk rows (0/negative-rate inputs across both tables) could get a one-time
data-quality flag/quarantine. Deliberately NOT folded into the observational
backfill (which must mirror the writer). Decide separately if/when the radar surface
needs it beyond the read-filter.
