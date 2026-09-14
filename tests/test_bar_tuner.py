"""Unit tests for decisions/bar_tuner.py. Pure functions: no DB, no Flask."""
from decisions.bar_tuner import (
    BARS, DUP_EXACT_SECONDS, DUP_NEAR_SECONDS, MIN_OFFERS, MIN_SHIFTS,
    QUEUE_SECONDS, SHIFT_GAP_SECONDS, SMOOTH_WINDOW,
    dedupe, net_hourly, replay, split_shifts, tuner,
)


def offer(t, fare, miles, minutes):
    return {"t": t, "fare": fare, "miles": miles, "minutes": minutes}


# --- scoring --------------------------------------------------------------

def test_net_hourly_matches_a_logged_engine_verdict():
    # decision_log 13482: $3.30, 3.8 committed mi, 10 committed min, cost 0.18
    # -> decision_result.netHourlyUsd = 15.7
    assert round(net_hourly(3.30, 3.8, 10, 0.18), 1) == 15.7


def test_net_hourly_is_none_without_a_duration():
    assert net_hourly(10.0, 5.0, 0, 0.18) is None


def test_cost_per_mile_is_applied_to_every_mile():
    # $20 fare, 10 mi, 30 min: at 0.18 -> (20-1.8)/0.5 = 36.4 ; at 0.36 -> 32.8
    assert round(net_hourly(20.0, 10.0, 30, 0.18), 2) == 36.40
    assert round(net_hourly(20.0, 10.0, 30, 0.36), 2) == 32.80


# --- duplicates -----------------------------------------------------------

def test_a_recapture_seconds_later_with_drifted_pickup_miles_is_one_offer():
    # decision_log 13252/13253: $8.00, 12.8 vs 12.2 mi, 25 min, 31 s apart
    assert len(dedupe([offer(0, 8.00, 12.8, 25), offer(31, 8.00, 12.2, 25)])) == 1


def test_an_exact_copy_within_the_hour_is_one_offer():
    # decision_log 12734/12735: $11.14, 18.9 mi, 26 min, 16.6 min apart
    assert len(dedupe([offer(0, 11.14, 18.9, 26), offer(996, 11.14, 18.9, 26)])) == 1


def test_the_first_copy_is_the_one_kept():
    kept = dedupe([offer(31, 8.00, 12.2, 25), offer(0, 8.00, 12.8, 25)])
    assert [(o["t"], o["miles"]) for o in kept] == [(0, 12.8)]


def test_a_different_fare_is_never_a_duplicate():
    assert len(dedupe([offer(0, 8.00, 12.8, 25), offer(20, 8.01, 12.8, 25)])) == 2


def test_same_fare_but_a_different_trip_shortly_after_is_kept():
    assert len(dedupe([offer(0, 3.30, 2.5, 11), offer(60, 3.30, 4.8, 11)])) == 2


def test_near_matches_outside_the_short_window_are_kept():
    assert len(dedupe([offer(0, 3.30, 3.3, 11), offer(DUP_NEAR_SECONDS + 1, 3.30, 3.2, 11)])) == 2


def test_identical_cheap_fares_on_different_days_are_kept():
    # 13310 (Sep 1) and 13402 (Sep 9): both $3.30 / 3.2 mi / 11 min, different pickups
    assert len(dedupe([offer(0, 3.30, 3.2, 11), offer(DUP_EXACT_SECONDS + 1, 3.30, 3.2, 11)])) == 2


def test_tuner_reports_how_many_duplicates_it_removed():
    offers = many_shifts()
    copies = [dict(o, t=o["t"] + 15) for o in offers[:5]]
    body = tuner(offers + copies, 0.18, 14.0)
    assert body["duplicatesRemoved"] == 5
    assert body["offers"] == len(offers)


# --- shifts ---------------------------------------------------------------

def test_a_long_gap_starts_a_new_shift():
    offers = [offer(0, 10, 3, 10), offer(600, 10, 3, 10),
              offer(600 + SHIFT_GAP_SECONDS + 1, 10, 3, 10)]
    assert [len(s) for s in split_shifts(offers)] == [2, 1]


def test_shifts_are_built_from_time_order_whatever_the_input_order():
    offers = [offer(1200, 10, 3, 10), offer(0, 10, 3, 10), offer(600, 10, 3, 10)]
    assert [o["t"] for o in split_shifts(offers)[0]] == [0, 600, 1200]


# --- replay ---------------------------------------------------------------

def test_offers_arriving_while_busy_are_skipped():
    # Ride 1 at t=0 lasts 30 min. An equally good offer at t=10 min must be
    # skipped (driver is on a trip); one at t=31 min is taken.
    good = dict(fare=20.0, miles=5.0, minutes=30)
    shifts = split_shifts([offer(0, **good), offer(600, **good), offer(1860, **good)])
    r = replay(shifts, 0.18, 10.0)
    assert r["rides"] == 2
    assert r["passPct"] == 100  # all three CLEAR the bar; only two can be driven


def test_an_offer_in_the_last_minutes_of_a_trip_is_queued_and_starts_at_dropoff():
    # Ride 1: t=0, 30 min, ends at 1800. Ride 2 offered 2 min before dropoff.
    good = dict(fare=20.0, miles=5.0, minutes=30)
    shifts = split_shifts([offer(0, **good), offer(1800 - 120, **good)])
    r = replay(shifts, 0.18, 10.0)
    assert r["rides"] == 2
    assert r["onlineHours"] == 1.0     # 0 -> 1800 -> 3600: ride 2 starts at dropoff
    assert r["busyHours"] == 1.0


def test_an_offer_earlier_than_the_queue_window_is_still_skipped():
    good = dict(fare=20.0, miles=5.0, minutes=30)
    shifts = split_shifts([offer(0, **good), offer(1800 - QUEUE_SECONDS - 1, **good)])
    assert replay(shifts, 0.18, 10.0)["rides"] == 1


def test_a_burst_of_queued_offers_still_gives_one_ride_per_trip():
    good = dict(fare=20.0, miles=5.0, minutes=30)
    burst = [offer(1800 - 150, **good), offer(1800 - 100, **good), offer(1800 - 50, **good)]
    r = replay(split_shifts([offer(0, **good)] + burst), 0.18, 10.0)
    assert r["rides"] == 2


def test_a_higher_bar_never_takes_more_rides_than_a_lower_one_from_idle():
    shifts = split_shifts([offer(i * 3600 // 4, 8 + i, 3, 12) for i in range(12)])
    rides = [replay(shifts, 0.18, b)["rides"] for b in (10, 20, 30, 40, 60)]
    assert rides == sorted(rides, reverse=True)


def test_online_time_runs_to_the_end_of_the_last_ride():
    # One shift: a 60-minute ride taken at t=0 and nothing else -> 1.0 online hour.
    r = replay(split_shifts([offer(0, 40.0, 10.0, 60)]), 0.18, 10.0)
    assert r["onlineHours"] == 1.0
    assert r["busyHours"] == 1.0
    assert r["perOnlineHour"] == round(40.0 - 1.8, 2)


def test_per_online_hour_counts_the_wait_that_per_busy_hour_ignores():
    # Two offers 60 min apart in one shift, each a 30-minute ride.
    # Taking both: 1.5 h online (0 -> 90 min), 1.0 h busy.
    shifts = split_shifts([offer(0, 20.0, 5.0, 30), offer(3600, 20.0, 5.0, 30)])
    r = replay(shifts, 0.18, 10.0)
    assert r["busyHours"] == 1.0
    assert r["onlineHours"] == 1.5
    assert r["perBusyHour"] > r["perOnlineHour"]


def test_offers_without_duration_are_counted_but_never_taken():
    shifts = split_shifts([offer(0, 20.0, 5.0, 0), offer(60, 20.0, 5.0, 30)])
    r = replay(shifts, 0.18, 10.0)
    assert r["rides"] == 1


# --- response -------------------------------------------------------------

def many_shifts(n_shifts=MIN_SHIFTS, per_shift=MIN_OFFERS):
    offers, t = [], 0
    for _ in range(n_shifts):
        for i in range(per_shift):
            offers.append(offer(t, 10.0 + (i % 7) * 3, 4.0, 15))
            t += 20 * 60
        t += SHIFT_GAP_SECONDS * 3
    return offers


def test_too_little_data_returns_no_rows_rather_than_a_misleading_curve():
    body = tuner([offer(0, 20, 5, 30)], 0.18, 14.0)
    assert body["enoughData"] is False
    assert body["rows"] == []
    assert body["bestBar"] is None


def test_enough_data_returns_one_row_per_bar_in_order():
    body = tuner(many_shifts(), 0.18, 14.0)
    assert body["enoughData"] is True
    assert [r["bar"] for r in body["rows"]] == BARS
    assert body["currentBar"] == 14.0 and body["costPerMile"] == 0.18


def test_best_bar_is_the_highest_smoothed_value_and_ties_go_low():
    body = tuner(many_shifts(), 0.18, 14.0)
    top = max(r["perOnlineHourSmoothed"] for r in body["rows"])
    lowest_top = min(r["bar"] for r in body["rows"] if r["perOnlineHourSmoothed"] == top)
    assert body["bestBar"] == lowest_top


def test_smoothing_averages_the_bars_within_the_window():
    body = tuner(many_shifts(), 0.18, 14.0)
    rows = body["rows"]
    i = BARS.index(14.0)
    near = [r["perOnlineHour"] for r in rows if abs(r["bar"] - 14.0) <= SMOOTH_WINDOW]
    assert rows[i]["perOnlineHourSmoothed"] == round(sum(near) / len(near), 2)


def test_a_one_bar_spike_does_not_become_the_best_bar(monkeypatch):
    # Flat $12 everywhere except a lone $15 at $17 - the shape seen in real data.
    import decisions.bar_tuner as bt
    def fake_replay(shifts, cost, bar):
        return {"bar": bar, "passPct": 50, "rides": 10, "net": 100.0, "busyHours": 5.0,
                "onlineHours": 8.0, "perOnlineHour": 15.0 if bar == 17.0 else 12.0,
                "perBusyHour": 20.0}
    monkeypatch.setattr(bt, "replay", fake_replay)
    body = bt.tuner(many_shifts(), 0.18, 14.0)
    assert body["bestBar"] != 17.0
