# DSI — Drive Score Index — CANONICAL SPECIFICATION

**Status:** LIVE (observational). Steps 1-2 shipped. The lynchpin metric for
PuddleJumper decision-making and marketing.
**Owners:** Andrew (decisions) · Gemini (review) · CC (implement) · Claude (spec)
**Originated:** 2026-06-02 · **Last consolidated:** 2026-06-02 (this is the
authoritative current-state record; supersedes all prior layered drafts)

> **READ THIS FIRST.** Earlier versions of this document accreted in layers as the
> concept was discovered and validated over one session. This v2 is reconciled:
> every statement below reflects CURRENT state and the SHIPPED code. Where an early
> idea was later superseded (the `dsi_standard` name, the absolute-only threshold,
> the optimization cron), the superseded version has been removed or explicitly
> marked. This document is the single source of truth; the code (`dsi.py`) is the
> authority on the formula.

---

## 1. THE FORMULA (canonical — verified against `dsi.py`)

```
DSI = effective_hourly_rate + DSI_MILE_WEIGHT × (dollars_per_mile − IRS_RATE_PER_MILE)
```

Computed by `compute_dsi_v1(effective_hourly_rate, dollars_per_mile)` in `dsi.py`.

**Constants (named, defined once in `dsi.py`, never inlined):**
- `IRS_RATE_PER_MILE = 0.725` — 2026 IRS business mileage rate. A STANDARDIZATION
  ANCHOR, not a cost estimate (see §3.1). Updates annually → versioned column.
- `DSI_MILE_WEIGHT = 12` — the weighting multiplier that bridges the per-mile margin
  onto the hourly scale. A deliberate PRODUCT PARAMETER (judgment call), NOT
  data-derived (§5) and NOT a months-amortization.

**Inputs are Uber's displayed card values, captured verbatim** (`router.py:561-562`,
`p.get("hourlyRate")` / `p.get("dollarsPerMile")`). DSI does NOT recompute rates
from `fare ÷ time`. Forbid any future "improvement" that does — it would diverge
from both Uber's number and the historical column.

**Plain-language meaning (the marketing sentence):** take the gross hourly rate,
then reward or penalize it by how far the per-mile rate beats or trails the cost of
running the car. A ride that looks great hourly but burns unpaid miles is marked
down; a lower-hourly ride with fat per-mile margin is marked up. *One number that
sees the trade-off two separate thresholds cannot.*

---

## 2. THE TWO VARIANTS — Standard vs Personal

**Standard DSI** (subtract `IRS_RATE_PER_MILE` = 0.725): the STORED, cross-driver
comparable value. One common ruler so every driver's DSI is on the same scale. This
is what lives in the database columns and the market/radar surface.

**Personal DSI** (subtract the driver's own `cost_per_mile`): the runtime value for
THIS driver. Corrects the inflated IRS anchor to the driver's real economics. NOT
stored.

**Implementation — read-time algebra (Gemini ruling, NOT recompute-from-scratch):**
because cost-per-mile enters linearly, personal differs from standard by a FIXED
SCALAR:
```
Δ = DSI_MILE_WEIGHT × (IRS_RATE_PER_MILE − driver_cost_per_mile)
Personal DSI = Standard DSI + Δ
```
For a driver at 0.45/mi: `Δ = 12 × (0.725 − 0.45) = +3.3`. The engine reads the
stored/interpolated STANDARD DSI and adds Δ. ONE radar channel, one addition — no
separate personal column, no triple-channel interpolation.

> **UNVERIFIED INPUT (must confirm before personal DSI drives anything):** the
> driver cost-per-mile (Andrew ≈ 0.45) is ASSERTED, not yet read from user settings.
> The entire Δ offset depends on it. Confirm from the settings table first.

---

## 3. CONCEPTUAL FOUNDATION (the marketing + design spine)

DSI's value rests on TWO principles. Keeping them distinct is what makes the case
un-attackable.

**Principle 1 — Market-relativity.** "Lousy" is meaningless in the abstract. A
$0.50/mi offer is not bad if the going rate for THIS place/time IS $0.50/mi — it's
market. The question is "is this at or above the local market rate," never "is this
good absolutely." NOTE: the legacy two-surface radar is ALSO market-relative — so
market-relativity is NOT DSI's unique edge. (Do not market "DSI is relative and the
old way isn't" — false, and a sharp reader breaks it.)

**Principle 2 — Tradeoff-awareness (THIS is DSI's actual edge).** The legacy engine
combines its two relative comparisons with a brittle **AND gate**: a ride must beat
market on hourly AND on mileage. A ride far above market hourly but a penny below
market mileage FAILS the AND and is declined — though its blend is excellent. DSI
collapses to one number where strength on one axis compensates weakness on the
other. **A single DSI comparison structurally cannot produce that leak.** Proven by
the 20 Freestyle disagreement rows (§4.2).

**Bulletproof one-line claim:** *DSI is market-relative AND tradeoff-aware in a
single number.*

### 3.1 Why the IRS 0.725 is an anchor, not a cost (settles the "it's inflated" point)
0.725 is a tax-deduction rate — deliberately generous, averaged across all
vehicles. It almost certainly OVERSTATES a given driver's true marginal cost (a
paid-off efficient car runs well under it). That is FINE and intended: the standard
DSI's job is a common ruler, not per-driver accuracy. The PERSONAL variant (§2)
corrects the inflation by subtracting the driver's real cost — that IS the +Δ. So
"the IRS number is inflated" is not a reason to change the stored anchor; it is the
reason the personal variant exists. Cross-driver comparability requires one fixed
anchor; lowering it would make the surface accurate to one driver and useless to
all.

### 3.2 DSI formalizes the operator's gut override (behavioral validation)
When an offer has high $/hr but failing $/mi (or vice versa), Andrew frequently
overrides the engine's DECLINE and drives it. The 20 Freestyle rows are those
overrides, quantified. DSI AUTOMATES that judgment — which is why it "feels
sensible." **Caveat — UNCONFIRMED:** the DB records engine verdict + PUDO detection,
NOT Uber-confirmed completed rides. "I drive the declined ride" is operator
recollection, a strong hypothesis, not data-proven. The Uber export (§7) is what
confirms it.

---

## 4. VALIDATION — what is PROVEN vs what is GATED

### 4.1 PROVEN from data (pre-build, read-only, 8 months `offer_history`)
- **Varies** across time/zone: DSI 15.1 → 31.2 across day×time buckets (~2x).
- **Separates from $/hr:** mileage term swings −3.6 (mi<0.725) to +4.4 (mi≥0.725),
  an 8-pt swing. DSI is NOT collinear with hourly — the weight surfaces real signal.
- **Hidden-cost thesis quantified:** 1456 of 2567 offers (57%) fall BELOW the 0.725
  mile line — decent hourly ($15.5 avg) masking mileage cost that drags true DSI to
  12.0. This is the core marketable insight.
- **Orders rides monotonically** (community_offers band check): higher DSI = higher
  hourly territory, band over band. Safe as an ordering signal.
- **Beats market, weight-independently:** engine-accepted offers score +9 to +11
  DSI above all-offer market DSI at EVERY weight 8-16. This is the "beat the market"
  proof — provable from `community_offers` alone, no Uber export needed.

### 4.2 The engine-defect evidence (the 20 Freestyle rows)
In FREESTYLE mode (pure economics, no positioning mandate), the legacy two-floor
gate declined ~20 economically strong rides on a single-axis floor — e.g. $44.7/hr
declined for $0.54/mi ("mileage rate too low"), $24.8/hr + $1.53/mi declined for
"hourly too low." These are exactly the trades DSI's blend would take. This is the
concrete defect DSI fixes.

### 4.3 GATED — must calibrate before DSI DRIVES verdicts (not before it's marketed)
- **Threshold** — DATA-SUGGESTED at ~20-25 (Freestyle accepts mean ~30, declines
  mean ~13; the cut sits in the trough). Finalize in shadow mode. PROVISIONAL:
  red < 20 / amber 20-25 / green > 25.
- **Comparator rule** — see §6 (the relative market-anchored model). The endpoint it
  needs does not yet exist.
- **Weight** — SETTLED as a judgment call (§5); not a calibration blocker.

---

## 5. WEIGHT CALIBRATION — CONCLUSIVE NEGATIVE RESULT

Two independent objectives tested whether `DSI_MILE_WEIGHT` is data-derivable:
- **Maximize accepted-vs-market spread:** climbs monotonically 9.04→11.21 across
  weights 8-16. No peak.
- **Maximize ACCEPT-vs-DECLINE separation (Freestyle):** climbs monotonically
  16.6→18.3 across 10-14. No peak. Scatter rises in lockstep both times.

**Conclusion:** with only $/hr and $/mi and no OUTCOME label, the weight has NO
data-derived optimum — higher weight just mechanically amplifies an existing
axis-gap. Therefore:
1. **`DSI_MILE_WEIGHT = 12` is a chosen product parameter, settled — not pending.**
2. **The weekly optimization cron is STRUCK** (not deferred). Do NOT build a
   weight-tuning cron on any rate-only objective. If ever built, it must be a
   PREDICTIVE model against real completed-ride OUTCOMES (Uber export) — the only
   objective that can be wrong in both directions and thus has a real optimum.
3. Optimizing against `app_verdict` is the WRONG TARGET anyway — it would tune DSI
   to mimic the flawed engine, including the 20 leak rows.

---

## 6. THE GO/NO-GO COMPARATOR (the real decision model)

Two models exist. Be explicit about which is interim and which is the design.

**INTERIM (shadow-mode v1, shippable now, no endpoint needed):** absolute threshold
— is the offer's DSI above ~20? Validated to separate accepts/declines. Fine for
putting a number+color on screen and watching behavior. **This is scaffolding.**

**THE REAL DESIGN (shadow-mode v2 — market-anchored + personal):**
```
GO  if  PERSONAL_offer_DSI  ≥  MARKET_DSI_at(location, now)
        where PERSONAL_offer_DSI = offer_standard_DSI + Δ      (Δ from §2)
        and   MARKET_DSI = IDW-interpolated standard DSI from the radar surface
              at the driver's exact coordinates (not H3-snap)
```
Rationale: a fixed threshold is WRONG in a structurally-low zone — an offer at
DSI-18 where the whole area pays DSI-15 is the best available and should be taken; a
flat "≥20" would strand the driver idle. The cut must MOVE with the local market.
That is the entire reason the radar surface exists.

**BLOCKER:** the radar-DSI endpoint does NOT yet exist. The legacy `get_price_radar()`
returns hourly/mileage channels; it must be extended (or a sibling endpoint built)
to return an interpolated STANDARD DSI at a coordinate, applying the §6.1 read
filter. Until that endpoint ships, only the interim absolute model can run. Confirm
the radar's live signature against the code at build — do not assume from memory.

### 6.1 Radar read-filter (REQUIRED on the consumed surface)
- **CORRECT (input sanity):** exclude rows where `effective_hourly_rate <= 0 OR
  dollars_per_mile <= 0` (impossible = capture error). Apply project bounds
  (`< 150` / `< 10`).
- **WRONG — never use `dsi_v1 > 0`:** that deletes the ~94 legitimately-negative
  real money-loser offers, which ARE the core signal. Filter on INPUT validity, not
  DSI sign.
- Market signal = ALL offers (nail_it + reported), not just confirmed — declined
  offers are valid market signal (Uber priced them; someone drove them).

---

## 7. THE UBER EXPORT — on the critical path

PuddleJumper's DB holds DECISIONS (engine verdict, PUDO detection), NOT OUTCOMES
(what was actually driven + earned). Two things need the outcome label and ONLY the
Uber export provides it:
1. **Honest weight optimization** (§5) — a predictive model on real outcomes.
2. **The revenue backtest** — "of rides I drove, what did DSI-flagged ones actually
   pay." (The "beat the market %" claim in §4.1 does NOT need it — that's
   offer-vs-offer and already provable.)

Request via myprivacy.uber.com/privacy/exploreyourdata/download (official, 1-3 day
ZIP; per-trip fare + distance + times). Avoid third-party scrapers — account-ban
risk. Join to `offer_history` by timestamp + coords (no shared offer IDs).

---

## 8. SCHEMA & DATA STATE (shipped)

- Column: **`dsi_v1`** (double precision, nullable) on BOTH `app_private.offer_history`
  and `public.community_offers`. Versioned (`_v1`) because the formula has volatile
  inputs (annual IRS rate, the weight) — a flat column would become a mixed dataset.
  (This resolved former open-question #1 in favor of versioning.)
- **Step 1 (schema + named constants + ingest writer):** SHIPPED, deployed rev
  00644-w4s. Writer is NULL-strict (both rates present → compute; either NULL →
  NULL), observational only — NO verdict path reads `dsi_v1` (enforced by test).
- **Step 2 (backfill):** committed 0884865. All 4093 historical rows carry `dsi_v1`
  (writer-parity proven, idempotent, single-txn). Distribution: offer_history avg
  20.0, community_offers avg 20.7.
- **Data-quality finding:** ~273 junk rows (0/negative rate inputs = capture errors)
  exist in the ledger. Kept writer-faithful in storage; EXCLUDED at radar-read via
  §6.1. The ~94 legitimately-negative-DSI rows are real signal and KEPT.

---

## 9. INTERFACE — deliberately minimal

- **Visual (always on):** ONE DSI number + a color under the frog. Red < 20 / amber
  20-25 / green > 25 (PROVISIONAL — tuned in shadow mode). No axis breakdown — the
  single number+color IS the interface. (A driver at speed cannot process a two-axis
  tradeoff; the personal-cost adjustment re-sorts collided rides before the number
  hits the screen.)
- **Amber/orange for low DSI, NOT red** — the unexplained dropoff "red screen" alarm
  (8851, 8857, 9007) is still undiagnosed; do not stack a second red signal. Revisit
  the color only after that alarm is resolved.
- **Vocal (optional, OFF by default):** may speak the number; assumed inaudible on
  busy nights, NEVER load-bearing.
- **Live vs historical toggle:** live local market DSI (sparse with one driver today)
  vs historical aggregate (8 months, dense). Default historical until community
  volume makes live stable.

### 9.1 The single number IS the product thesis
The value is not the formula (copyable in minutes) — it is the LIVE VISIBILITY of
one honest, market-relative, tradeoff-aware number answering *"Is where I am right
now any good?"* Red = move, green = stay. The moat is the DATA + COMMUNITY, not the
arithmetic.

---

## 10. MARKETING FRAME (the lynchpin claims)

- **Publish the PREMISE and PRINCIPLE; hold the exact weight and the data.** The
  formula is not protectable (three operations); the data surface + community +
  "beat the market" proof ARE. Brand the DATA as the proprietary intelligence, not
  the math.
- **Hero claim (provable now):** *"Drivers who follow DSI beat the market by X%"* —
  from `community_offers` (accepted DSI vs all-offer DSI). State it as DSI-spread,
  NOT verified take-home pay, until the Uber export backs the stronger version.
- **The insight series (safe to publish now, no product dependency):** (1) the hidden
  per-mile cost; (2) a bad $/mile ride can still be great — SHORT trips barely use
  miles (demonstrate in DOLLARS, not by switching units); (3) a great $/mile ride
  can be a trap — long slow hauls. Then (4) introduce DSI as the one number that does
  this; (5) the live "is your zone any good" readout.
- **Honesty guardrail:** every published claim must stay inside what the data proves.
  The insight pieces are observations (safe). The "beat market" claim must keep its
  exact methodology defensible — driver subreddits will fact-check hard.

---

## 11. BUILD ARC (current)

1. ✅ Schema + constants + writer (rev 00644-w4s).
2. ✅ Backfill (commit 0884865, 4093 rows).
3. ⏳ **Radar-DSI endpoint** (§6) — interpolated market DSI at a coordinate, §6.1
   filter. PREREQUISITE for the real comparator and the live readout. Does not exist.
4. ⏳ **Android shadow-mode readout** — number + color under frog (amber-not-red),
   radar lookup. Gated on step 3. CC owns this codebase.
5. ⏳ **Confirm driver cost-per-mile** from settings (§2) before personal DSI is live.
6. ⏳ **Calibrate threshold** in shadow mode against live behavior (§4.3).
7. ⏳ **Uber export** (§7) → honest weight tuning + revenue backtest.
8. ❌ **Weight-optimization cron — STRUCK** (§5). Not on the roadmap as a rate-only
   objective.

---

## 12. NAMING & OPEN ITEMS
- **Canonical name: "Drive Score Index" (DSI).** The code docstring's "Driver
  Standard Index" is WRONG — being corrected. All marketing + docs + code must agree.
- Open (Gemini, low-priority): confirmed already — column versioned (`dsi_v1`),
  comparator is personal-vs-market (§6), sparse rows → NULL (§8).
- Open (Android, non-blocking): confirm the client sends Uber's raw card rates and
  doesn't recompute them.
