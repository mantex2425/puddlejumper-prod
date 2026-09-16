"""Unit tests for decisions/bar_tuner.py. Pure functions: no DB, no Flask."""
from decisions.bar_tuner import (
    BARS, DUP_EXACT_SECONDS, DUP_NEAR_SECONDS, MIN_OFFERS, MIN_SHIFTS,
    QUEUE_SECONDS, SHIFT_GAP_SECONDS, SMOOTH_WINDOW,
    DEFAULT_MIN_GROSS_HOURLY, NEEDED_RANGE_MIN_OFFERS, dedupe, needed_adder, net_hourly, replay,
    shown_gross, split_shifts, tuner,
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


def test_best_bar_is_within_tolerance_of_the_top_and_nearest_the_current_bar():
    body = tuner(many_shifts(), 0.18, 14.0)
    top = max(r["perOnlineHourSmoothed"] for r in body["rows"])
    tied = [r["bar"] for r in body["rows"] if top - r["perOnlineHourSmoothed"] <= 0.05 + 1e-9]
    assert body["bestBar"] == min(tied, key=lambda b: (abs(b - 14.0), b))


def test_a_flat_stretch_does_not_recommend_the_lowest_bar(monkeypatch):
    # $8-$13 identical (what an $18 minimum does to real data), $14 a little lower.
    import decisions.bar_tuner as bt
    def fake_replay(shifts, cost, bar, gross_floor=0.0):
        v = 15.14 if bar <= 13.0 else 14.0
        return {"bar": bar, "passPct": 50, "rides": 10, "net": 100.0, "busyHours": 5.0,
                "onlineHours": 8.0, "perOnlineHour": v, "perBusyHour": 20.0}
    monkeypatch.setattr(bt, "replay", fake_replay)
    # Smoothing averages $1 either side, so $12.50-$13 blend in the lower $14 values; $12 is
    # the tied bar nearest $14. Before this rule the answer was $8.
    assert bt.tuner(many_shifts(), 0.18, 14.0)["bestBar"] == 12.0
    assert bt.tuner(many_shifts(), 0.18, 10.0)["bestBar"] == 10.0     # already on the flat


def test_smoothing_averages_the_bars_within_the_window():
    body = tuner(many_shifts(), 0.18, 14.0)
    rows = body["rows"]
    i = BARS.index(14.0)
    near = [r["perOnlineHour"] for r in rows if abs(r["bar"] - 14.0) <= SMOOTH_WINDOW]
    assert rows[i]["perOnlineHourSmoothed"] == round(sum(near) / len(near), 2)


def test_a_one_bar_spike_does_not_become_the_best_bar(monkeypatch):
    # Flat $12 everywhere except a lone $15 at $17 - the shape seen in real data.
    import decisions.bar_tuner as bt
    def fake_replay(shifts, cost, bar, gross_floor=0.0):
        return {"bar": bar, "passPct": 50, "rides": 10, "net": 100.0, "busyHours": 5.0,
                "onlineHours": 8.0, "perOnlineHour": 15.0 if bar == 17.0 else 12.0,
                "perBusyHour": 20.0}
    monkeypatch.setattr(bt, "replay", fake_replay)
    body = bt.tuner(many_shifts(), 0.18, 14.0)
    assert body["bestBar"] != 17.0


# --- on-screen translation -------------------------------------------------

def test_needed_adder_is_cost_times_committed_speed():
    # 30 mi in 60 min at $0.18 -> 5.40; 10 mi in 60 min -> 1.80; 20 mi in 30 min -> 7.20
    offers = [offer(0, 20, 30, 60), offer(600, 20, 10, 60), offer(1200, 20, 20, 30)]
    a = needed_adder(offers, 0.18)
    assert a["count"] == 3
    assert a["p50"] == 5.4
    assert a["p25"] == 3.6 and a["p75"] == 6.3   # linear interpolation


def test_needed_adder_uses_the_drivers_own_cost():
    offers = [offer(0, 20, 30, 60)]
    assert needed_adder(offers, 0.23)["p50"] == round(0.23 * 30, 4)


def test_needed_adder_uses_committed_minutes_including_any_return_leg():
    # The engine's committed minutes already include a modeled return; the adder must
    # use them as given, not recompute a shorter trip.
    with_return = needed_adder([offer(0, 20, 12, 40)], 0.18)["p50"]
    without = needed_adder([offer(0, 20, 12, 30)], 0.18)["p50"]
    assert with_return < without


def test_offer_clears_bar_exactly_when_gross_reaches_bar_plus_adder():
    fare, miles, minutes, cost, bar = 16.0, 9.0, 40.0, 0.18, 14.0
    gross = fare / (minutes / 60)
    adder = cost * miles / (minutes / 60)
    assert (net_hourly(fare, miles, minutes, cost) >= bar) == (gross >= bar + adder)


def test_tuner_adds_the_range_without_changing_the_replay():
    offers = many_shifts()
    body = tuner(offers, 0.18, 14.0)
    assert body["neededRangeMinOffers"] == NEEDED_RANGE_MIN_OFFERS == 20
    assert body["neededAdder"]["count"] == len(dedupe(offers))
    rows_again = tuner(offers, 0.18, 14.0)["rows"]
    assert body["rows"] == rows_again and body["bestBar"] == tuner(offers, 0.18, 14.0)["bestBar"]


def test_no_offers_means_no_range():
    assert needed_adder([], 0.18) is None
    assert tuner([], 0.18, 14.0)["neededAdder"] is None


def test_rows_report_what_passing_offers_showed_on_screen():
    # Three offers, all 60 min. Gross $/hr = fare. At a $10 bar with 2 mi each ($0.36 cost),
    # net = fare - 0.36: 9 fails, 20 and 30 clear -> median gross of passing offers 25.
    offers = [offer(0, 9.0, 2, 60), offer(4000, 20.0, 2, 60), offer(8000, 30.0, 2, 60)]
    r = replay(split_shifts(offers), 0.18, 10.0)
    assert r["passCount"] == 2
    assert r["passGrossMedian"] == 25.0
    assert r["passGrossP25"] == 22.5


def test_no_passing_offers_means_no_gross_median():
    r = replay(split_shifts([offer(0, 1.0, 5, 60)]), 0.18, 10.0)
    assert r["passCount"] == 0 and r["passGrossMedian"] is None


def test_gross_median_uses_committed_minutes_as_given():
    # Same fare, longer committed time (e.g. a modeled return) -> lower on-screen gross.
    short = replay(split_shifts([offer(0, 30.0, 2, 30)]), 0.18, 5.0)["passGrossMedian"]
    long_ = replay(split_shifts([offer(0, 30.0, 2, 45)]), 0.18, 5.0)["passGrossMedian"]
    assert long_ < short


# --- minimum on-screen gross (2026-09-16) ----------------------------------

def test_default_minimum_is_eighteen():
    assert DEFAULT_MIN_GROSS_HOURLY == 18.0


def test_shown_gross_matches_the_engine_including_the_exact_eighteen_case():
    assert shown_gross(3.30, 11) == 18.0          # not 17.99 from division noise
    assert shown_gross(6.01, 22) == 16.39
    assert shown_gross(5.60, 19) == 17.68


def test_floor_declines_an_offer_that_clears_the_bar_but_shows_too_little():
    # Sep 15 15:18: $6.01, 4.8 mi, 22 min -> net 14.03 clears $14; gross 16.39 < 18.
    shifts = split_shifts([offer(0, 6.01, 4.8, 22)])
    assert replay(shifts, 0.18, 14.0)["rides"] == 1
    assert replay(shifts, 0.18, 14.0, gross_floor=18.0)["rides"] == 0


def test_offer_exactly_at_the_floor_passes():
    assert replay(split_shifts([offer(0, 3.30, 2.5, 11)]), 0.18, 14.0, gross_floor=18.0)["rides"] == 1


def test_floor_zero_is_off_and_leaves_results_unchanged():
    offers = many_shifts()
    assert tuner(offers, 0.18, 14.0)["rows"] == tuner(offers, 0.18, 14.0, gross_floor=0.0)["rows"]


def test_tuner_reports_the_floor_and_passing_gross_respects_it():
    body = tuner(many_shifts(), 0.18, 14.0, gross_floor=40.0)
    assert body["grossFloor"] == 40.0
    for r in body["rows"]:
        if r["passGrossMedian"] is not None:
            assert r["passGrossP25"] >= 40.0
