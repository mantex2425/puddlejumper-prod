# DSI — Drive Score Index — CANONICAL SPECIFICATION

**Status:** LIVE (observational). Steps 1-2 shipped.
**Owners:** Andrew (decisions) · Gemini (review) · CC (implement) · Claude (spec)
**Originated:** 2026-06-02 · **Last updated:** 2026-06-11

### READ THIS FIRST
Single source of truth for the Drive Score Index. The code (`dsi.py`) is the final
authority on the implementation.

---

### 1. PURPOSE — THE QUESTION DSI ANSWERS

**"Right here, right now — this location, this day, this time — is this Uber offer
good compared to the actual local market?"**

The same Midtown corner can be worth ~$30/hr Saturday night and ~$15/hr Monday
morning. DSI respects that. When you're grinding at midnight, you don't want a
"maybe" — you want **Take it** or **Decline it**. DSI delivers one glanceable
number + color.

---

### 2. THE FORMULA (shipped — verified against `dsi.py`)

```
DSI = effective_hourly_rate + DSI_MILE_WEIGHT × (dollars_per_mile − IRS_RATE_PER_MILE)
```
Constants (defined once in `dsi.py`): `IRS_RATE_PER_MILE = 0.725`,
`DSI_MILE_WEIGHT = 12`.

**Inputs:** Uber's displayed `effective_hourly_rate` and `dollars_per_mile`, taken
verbatim — never recomputed from fare/time.

**Plain English:** combines hourly pay and mileage reality into one number. Strong
mileage boosts it; poor mileage penalizes it. Lands in the familiar ~$20-25 range.

---

### 3. TWO VARIANTS

- **Standard DSI** (stored): uses the 0.725 IRS anchor. The cross-driver-comparable
  value that powers the market radar.
- **Personal DSI** (runtime): adjusts Standard for the driver's real `cost_per_mile`.
  ```
  Δ = DSI_MILE_WEIGHT × (IRS_RATE_PER_MILE − driver_cost_per_mile)
  Personal DSI = Standard DSI + Δ
  ```

The IRS 0.725 is a standardization anchor, not a cost estimate — it's deliberately
high, so it overstates a cheap car's real cost. That's intended: the common anchor
keeps all drivers comparable; the Personal variant corrects it to the driver's
actual economics.

> **UNVERIFIED:** the driver `cost_per_mile` (Andrew ≈ 0.45) is asserted, not yet
> read from settings. Δ depends on it — confirm before Personal DSI drives anything.

---

### 4. COLOR CODING — DECISIVE, NO AMBER

- **Green (≥ 22):** Take it — at or above local market value.
- **Red (< 22):** Decline — below it.

No "maybe" zone — a binary call every time, by design (a tired driver needs a
decision, not a hedge).

> Threshold **22 is provisional** — data-suggested (the accept/decline trough sits
> ~20-25), to be finalized in shadow mode.
> **Android build note:** the undiagnosed dropoff "red-screen" alarm uses red too.
> Keep the DSI-decline red visually distinct from that alarm so the two aren't
> confused in the field.

---

### 5. WHY IT WORKS (two strengths)

1. **Market-relative** — compares the offer against real offers others are seeing in
   the same area and time, not an absolute bar.
2. **Tradeoff-aware** — strong hourly can offset weak mileage and vice versa. This is
   the real edge: the old engine used two separate floors joined by an AND gate, so
   it declined economically strong rides that missed one floor (proven on 20
   Freestyle rides — e.g. $44/hr declined for low $/mi). One DSI number can't make
   that mistake.

---

### 6. DECISION MODEL

- **Interim (now):** absolute threshold (22). Validated to separate good/poor offers.
  Scaffolding — fine for shadow mode.
- **Target:** `Personal DSI ≥ Local Market DSI`, where Local Market DSI is
  IDW-interpolated from crowdsourced offers at the driver's exact coordinates + time.
  A fixed threshold strands a driver in a structurally-low zone; the cut must move
  with the local market. **Blocker:** the radar-DSI endpoint doesn't exist yet.

**Radar read-filter (required when the endpoint is built):** exclude rows with
`effective_hourly_rate <= 0 OR dollars_per_mile <= 0` (capture-error junk). Do NOT
filter on `dsi_v1 > 0` — that would delete legitimately-negative real offers, which
are the core money-loser signal. Use ALL offers (declined included — they're valid
market signal).

---

### 7. VALIDATION — proven vs. gated

**Proven from 8 months of offers:** DSI varies by time/zone (15→31), separates from
$/hr (mileage term swings ±4), and 57% of offers fall below the IRS mile line (the
hidden-cost thesis). Engine-accepted offers beat all-offer market DSI by +9 to +11
at every weight — the "beat the market" proof, from crowdsource data alone.

**Settled:** the weight (12) is a chosen product parameter — two optimization
objectives proved it has no data-derived optimum, so no weight-tuning cron.

**Bounded by available data (no Uber export — decided):** the weight (12) stays a
permanent product parameter, and DSI makes QUALITY claims, not EARNINGS claims. We
can prove "DSI-accepted offers score X% above market" (offer-vs-offer, from
crowdsource data) — we cannot and do not claim "drivers earned X% more" (that would
need completed-ride outcomes the DB doesn't hold). The quality claim is the honest,
provable position.

---

### 8. MARKETING

**Positioning:** *"One number that answers: is this offer actually good here, right
now? Green = take it, Red = decline. No guessing while driving."*

Lead with the crowdsourced market intelligence and the back-tested "beat the market
by X%" claim. **The moat is the data + community, not the formula** (the math is
three operations — copyable; the market surface is not). State the beat-market claim
as DSI-spread, not verified take-home pay, until the Uber export backs the stronger
version.

---

### 9. REMAINING WORK
1. Radar-DSI endpoint (crowdsourced interpolation, §6 filter).
2. Android display — Green/Red (distinct from the alarm red).
3. Confirm driver cost-per-mile from settings.
4. Threshold calibration in shadow mode.

---

### 10. STATE & NAMING
- Column **`dsi_v1`** (versioned — the formula has volatile inputs) on
  `offer_history` + `community_offers`. Step 1 shipped (rev 00644-w4s); Step 2
  backfill done (commit 0884865, all 4093 rows). Observational only — no verdict path
  reads it.
- Canonical name: **Drive Score Index (DSI)**.
