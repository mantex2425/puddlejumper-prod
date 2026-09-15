"""bar_tuner.py -- what a different DSI bar would have done on this driver's own offers.

The bar (settings.dsi_threshold) is the single most consequential setting in the
app, and until now the only way to choose it was to guess, or to ask for a
hand-run SQL sensitivity. This replays the driver's real offers under a range of
bars so they can see the trade-off before changing it.

WHY A REPLAY, NOT A SIMPLE PASS COUNT. Scoring each offer against a bar and
summing the ones that pass says "$/hour on the rides you'd take" -- and that
number rises for ever as the bar rises, because it ignores the waiting between
rides. What a driver actually banks is dollars per hour ONLINE. So each shift is
replayed in time order: if the driver is free and the offer clears the bar, it
is taken and they are busy for its committed minutes; offers arriving while busy
are skipped, EXCEPT in the last QUEUE_SECONDS of a trip, when Uber queues the next
offer and it starts at dropoff. Offer arrival times are the real ones, so no arrival-rate model is
assumed. Measured on one driver's 30 days (2026-09-14): per-busy-hour climbed
from $17.50 at a $10 bar to $47.83 at $22, while per-online-hour was flat around
$12-13 up to $17 and fell after -- the flat line is the true answer.

KNOWN BIAS, disclosed in the app: offers are the ones actually received at the
bar the driver really used. At a HIGHER bar they would have been free more often
and seen offers this data does not contain, so high bars read conservatively.

SCORING matches decision_engine_v3 exactly, re-priced at the driver's CURRENT
cost per mile: net_hourly = (grossPayout - committedMiles * cost) / (committedMinutes / 60).
Checked against a logged verdict: $3.30, 3.8 committed mi, 10 committed min, cost
0.18 -> 15.70, identical to the engine's netHourlyUsd. Offers scored before a cost
change are re-scored at today's cost, so the whole window is comparable.

DUPLICATES. The same offer card is sometimes captured and logged more than once
(re-captures while it is on screen, and testing), each with its own offer_id, so
ids cannot collapse them. Measured on 2026-09-14: 13 pairs in 325 offers had the
same fare 15-80 s apart with committed miles differing by 0.1-1.0 (pickup miles
move as the car moves), and one was an exact copy 16 min later. dedupe() keeps the
first of each. Identical cheap fares days apart were checked and are genuinely
different trips (different pickups in the OCR), so the exact-copy window is short.

QUEUED OFFERS. Uber starts sending the next offer as a trip nears its end, and the
driver can accept it. Measured 2026-09-14 on the same 30 days: the offer stream
goes silent for 101 gaps over 10 minutes (the rides actually driven; in 90 of them
the offer just before the silence cleared the $14 bar), and the next offer arrived
a median 0.3 min after that ride's committed minutes ran out. With no queue window
the replay skipped those offers and gave 95 rides at $14; with 3 minutes it gives
101, matching the rides visible in the stream (5 minutes gave 108). A queued offer
can only be accepted once, so a burst of offers during one trip still yields one
ride.

Only the bar is modelled. Declines for other reasons (maximum pickup distance, red
zones) are not replayed.
"""
from __future__ import annotations

QUEUE_SECONDS = 3 * 60              # an offer this close to the current trip's end can be accepted
SHIFT_GAP_SECONDS = 60 * 60          # a gap longer than this between offers starts a new shift
# "Best bar" is chosen on dollars per online hour averaged over bars within this
# many dollars either side. Unsmoothed, the replay is spiky: on 2026-09-14 a single
# $17 row read $13.21 between $11.77 at $16 and $12.04 at $18, and would have told
# a driver to raise their bar on noise.
SMOOTH_WINDOW = 1.0
BARS = [b / 2 for b in range(16, 61)]  # $8.00 .. $30.00 in $0.50 steps
DUP_NEAR_SECONDS = 3 * 60             # same fare, near-same miles/minutes within this = one offer
DUP_NEAR_MILES = 1.0
DUP_NEAR_MINUTES = 2.0
DUP_EXACT_SECONDS = 60 * 60          # identical fare, miles and minutes within this = one offer
MIN_OFFERS = 30
# Below this many offers the app shows only the median on-screen need, not a range.
NEEDED_RANGE_MIN_OFFERS = 20
MIN_SHIFTS = 3


def net_hourly(fare: float, miles: float, minutes: float, cost_per_mile: float) -> float | None:
    """The engine's net hourly for one offer, or None if it has no duration."""
    if minutes is None or minutes <= 0:
        return None
    return (fare - miles * cost_per_mile) / (minutes / 60.0)


def is_duplicate(earlier: dict, later: dict) -> bool:
    """True if `later` is a re-logged copy of `earlier` (see DUPLICATES above)."""
    gap = later["t"] - earlier["t"]
    if gap < 0 or round(earlier["fare"], 2) != round(later["fare"], 2):
        return False
    if gap <= DUP_EXACT_SECONDS and earlier["miles"] == later["miles"] and earlier["minutes"] == later["minutes"]:
        return True
    return (gap <= DUP_NEAR_SECONDS
            and abs(earlier["miles"] - later["miles"]) <= DUP_NEAR_MILES + 1e-9
            and abs((earlier["minutes"] or 0) - (later["minutes"] or 0)) <= DUP_NEAR_MINUTES + 1e-9)


def dedupe(offers: list[dict]) -> list[dict]:
    """Time-ordered offers with re-logged copies removed; the first copy is kept."""
    kept: list[dict] = []
    for o in sorted(offers, key=lambda o: o["t"]):
        recent = (k for k in reversed(kept) if o["t"] - k["t"] <= DUP_EXACT_SECONDS)
        if not any(is_duplicate(k, o) for k in recent):
            kept.append(o)
    return kept


def split_shifts(offers: list[dict]) -> list[list[dict]]:
    """Group time-ordered offers into shifts separated by > SHIFT_GAP_SECONDS."""
    shifts: list[list[dict]] = []
    current: list[dict] = []
    for o in sorted(offers, key=lambda o: o["t"]):
        if current and o["t"] - current[-1]["t"] > SHIFT_GAP_SECONDS:
            shifts.append(current)
            current = []
        current.append(o)
    if current:
        shifts.append(current)
    return shifts


def _percentile(sorted_xs: list[float], p: float) -> float:
    """Linear-interpolated percentile of an already-sorted list (0 <= p <= 1)."""
    if len(sorted_xs) == 1:
        return sorted_xs[0]
    k = (len(sorted_xs) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (k - lo)


def replay(shifts: list[list[dict]], cost_per_mile: float, bar: float) -> dict:
    """Replay every shift at one bar. See module docstring for the model."""
    offers_total = passing = rides = 0
    net = busy_s = online_s = 0.0
    pass_gross: list[float] = []   # on-screen gross $/hr of every offer that clears the bar
    for shift in shifts:
        busy_until = float("-inf")
        start = shift[0]["t"]
        end = start
        for o in shift:
            offers_total += 1
            n = net_hourly(o["fare"], o["miles"], o["minutes"], cost_per_mile)
            if n is None:
                continue
            clears = n >= bar
            if clears:
                passing += 1
                pass_gross.append(o["fare"] / (o["minutes"] / 60.0))
            if clears and o["t"] >= busy_until - QUEUE_SECONDS:
                starts = max(o["t"], busy_until)   # a queued offer starts at dropoff
                rides += 1
                net += o["fare"] - o["miles"] * cost_per_mile
                busy_s += o["minutes"] * 60
                busy_until = starts + o["minutes"] * 60
                end = max(end, busy_until)
            end = max(end, o["t"])
        online_s += end - start

    online_h = online_s / 3600.0
    busy_h = busy_s / 3600.0
    return {
        "bar": bar,
        "passPct": round(100.0 * passing / offers_total) if offers_total else 0,
        "rides": rides,
        "net": round(net, 2),
        "busyHours": round(busy_h, 1),
        "onlineHours": round(online_h, 1),
        "perOnlineHour": round(net / online_h, 2) if online_h > 0 else 0.0,
        "perBusyHour": round(net / busy_h, 2) if busy_h > 0 else 0.0,
        # What offers that clear this bar showed on screen (gross $/hr, the frog's number).
        # The median, not the mean: a few very large fares pull the mean up ($24.11 vs a
        # $21.46 median at $14 on 2026-09-15). Shown as "rides that pass this bar have paid
        # about $21/hr on screen" -- what passing work looks like, not the pass floor.
        "passCount": passing,
        "passGrossMedian": round(_percentile(sorted(pass_gross), 0.5), 2) if pass_gross else None,
        "passGrossP25": round(_percentile(sorted(pass_gross), 0.25), 2) if pass_gross else None,
    }


def needed_adder(offers: list[dict], cost_per_mile: float) -> dict | None:
    """What each offer adds on top of the bar to get the gross $/hr it needs on screen.

    An offer clears the bar exactly when gross $/hr >= bar + cost * committed mph, the
    same test the engine makes and the frog's "needs $X" line shows. The second term
    depends on the trip, not the bar, so bar + its 25th percentile is the on-screen rate
    at which short, slow trips can just pass ("some short, slow trips can pass near
    $17/hr"). It is a FLOOR, not a target; the headline is passGrossMedian per row. Uses committed miles and minutes, which include the
    modeled return leg whenever the engine included it.
    """
    xs = sorted(cost_per_mile * o["miles"] / (o["minutes"] / 60.0)
                for o in offers if o.get("minutes") and o["minutes"] > 0)
    if not xs:
        return None
    return {
        "p25": round(_percentile(xs, 0.25), 4),
        "p50": round(_percentile(xs, 0.50), 4),
        "p75": round(_percentile(xs, 0.75), 4),
        "count": len(xs),
    }


def tuner(offers: list[dict], cost_per_mile: float, current_bar: float | None,
          bars: list[float] = BARS) -> dict:
    """The full response body: one replayed row per bar, plus context."""
    received = len(offers)
    offers = dedupe(offers)
    shifts = split_shifts(offers)
    enough = len(offers) >= MIN_OFFERS and len(shifts) >= MIN_SHIFTS
    rows = [replay(shifts, cost_per_mile, b) for b in bars] if enough else []
    for r in rows:
        near = [x["perOnlineHour"] for x in rows if abs(x["bar"] - r["bar"]) <= SMOOTH_WINDOW + 1e-9]
        r["perOnlineHourSmoothed"] = round(sum(near) / len(near), 2)
    best = None
    if rows:
        # Highest SMOOTHED dollars per online hour; ties go to the LOWER bar, which
        # takes more rides for the same money and so carries less risk.
        best = max(rows, key=lambda r: (r["perOnlineHourSmoothed"], -r["bar"]))["bar"]
    return {
        "costPerMile": cost_per_mile,
        "currentBar": current_bar,
        "offers": len(offers),
        "duplicatesRemoved": received - len(offers),
        "shifts": len(shifts),
        "enoughData": enough,
        "minOffers": MIN_OFFERS,
        "minShifts": MIN_SHIFTS,
        "bestBar": best,
        "rows": rows,
        "neededAdder": needed_adder(offers, cost_per_mile),
        "neededRangeMinOffers": NEEDED_RANGE_MIN_OFFERS,
    }
