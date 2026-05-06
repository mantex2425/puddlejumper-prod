"""validate_gate_layer.py — kickoff scenario replay against real gate code.

Runs the 7 canonical scenarios from MOTION_GATE_KICKOFF.md against the
actual motion_gate.evaluate_gates and filter_matches_by_gates functions.
NOT a unit test — this is a live harness for confidence-building before
shipping. Run it after the apply script lands to confirm gate behavior
matches expectations end-to-end.

Usage from VM:
    cd ~/puddlejumper-prod
    python3 scripts/validate_gate_layer.py

Output is a table of pass/fail per scenario, with the gate verdict and
held offer/leg detail for forensic readability.

IMPORTANT — cumulative_miles semantics:

The kickoff doc's pseudocode (pre-dual-leg-design) used cumulative_miles
as TRIP-RELATIVE progress. The gate code uses cumulative_miles as the
DRIVER'S LIFETIME ODOMETER (the value that comes from
heartbeat_body['cumulative_miles']) and computes leg progress via
delta = cm - leg_start_cumulative_miles_*. This harness translates the
kickoff's trip-relative descriptions into absolute lifetime values:

  cm_absolute = leg_start_cumulative_miles_dropoff + trip_relative_progress

Synthetic scenarios use:
  - leg_start_cumulative_miles_pickup  = 0.0   (fictional baseline anchor)
  - leg_start_cumulative_miles_dropoff = pickup_miles  (driver fired pickup
                                                         after driving pickup_miles)
  - cm at any point = dropoff_anchor + (trip-relative miles since pickup_fire)
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Optional

# Make this runnable from puddlejumper-prod root.
sys.path.insert(0, ".")

from motion_gate import (
    GateVerdict,
    evaluate_gates,
    filter_matches_by_gates,
)


@dataclass
class FakeCluster:
    """Mirrors cluster_detection.Cluster shape (no speed field).

    Speed is sourced from the heartbeat request body in production
    (driver_heartbeat.py threads it into evaluate_gates(speed_mph=...)).
    Sprint A bugfix 2026-05-06: harness updated to match real Cluster
    shape so the synthetic and live behaviors stay coherent.
    """
    duration_s: float
    median_lat: float = 0.0
    median_lng: float = 0.0


@dataclass
class FakeOffer:
    id: int
    pickup_miles: Optional[float] = None
    trip_miles: Optional[float] = None
    leg_start_cumulative_miles_pickup: Optional[float] = None
    leg_start_cumulative_miles_dropoff: Optional[float] = None


@dataclass
class FakeMatch:
    offer_id: int
    location_type: str
    confidence: float = 0.85


# Drive 1 (offer 7711): pickup_miles=2.97, trip_miles=5.5
D1_PICKUP_MILES = 2.97
D1_TRIP_MILES = 5.5
D1_DROPOFF_ANCHOR = D1_PICKUP_MILES  # cm at fire_pickup

# Drive 2 (offer 7714): pickup_miles=2.97, trip_miles=30.8
D2_PICKUP_MILES = 2.97
D2_TRIP_MILES = 30.8
D2_DROPOFF_ANCHOR = D2_PICKUP_MILES

# Dentist drive: pickup_miles=0.5, trip_miles=1.9
DENT_PICKUP_MILES = 0.5
DENT_TRIP_MILES = 1.9
DENT_DROPOFF_ANCHOR = DENT_PICKUP_MILES


SCENARIOS = [
    {
        "label": "Drive 1 Stop 1A (mid-route 2-min light, ~1mi into 5.5mi trip)",
        "coords": (29.4975076, -95.518549),
        "duration_s": 120,
        "speed_mph": 0.0,
        "cumulative_miles": D1_DROPOFF_ANCHOR + 1.0,
        "offer": dict(
            id=7711, pickup_miles=D1_PICKUP_MILES, trip_miles=D1_TRIP_MILES,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=D1_DROPOFF_ANCHOR,
        ),
        "match": dict(offer_id=7711, location_type="dropoff"),
        "expect_match_passes": False,
        "expected_held_by": "odometer (dropoff leg)",
    },
    {
        "label": "Drive 1 Stop 1B (brief return near pickup, ~1.5mi trip-progress)",
        "coords": (29.4944391, -95.5038588),
        "duration_s": 60,
        "speed_mph": 0.0,
        "cumulative_miles": D1_DROPOFF_ANCHOR + 1.5,
        "offer": dict(
            id=7711, pickup_miles=D1_PICKUP_MILES, trip_miles=D1_TRIP_MILES,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=D1_DROPOFF_ANCHOR,
        ),
        "match": dict(offer_id=7711, location_type="dropoff"),
        "expect_match_passes": False,
        "expected_held_by": "odometer (dropoff leg)",
    },
    {
        "label": "Drive 1 Stop 1C (real dropoff at Excel Dental, full trip)",
        "coords": (29.5102826, -95.5272512),
        "duration_s": 180,
        "speed_mph": 0.0,
        "cumulative_miles": D1_DROPOFF_ANCHOR + D1_TRIP_MILES,
        "offer": dict(
            id=7711, pickup_miles=D1_PICKUP_MILES, trip_miles=D1_TRIP_MILES,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=D1_DROPOFF_ANCHOR,
        ),
        "match": dict(offer_id=7711, location_type="dropoff"),
        "expect_match_passes": True,
        "expected_held_by": None,
    },
    {
        "label": "Drive 2 final (real dropoff at US-90-ALT strip mall, ~30.5mi trip-progress)",
        "coords": (29.5912088, -95.6011899),
        "duration_s": 300,
        "speed_mph": 0.0,
        "cumulative_miles": D2_DROPOFF_ANCHOR + 30.5,
        "offer": dict(
            id=7714, pickup_miles=D2_PICKUP_MILES, trip_miles=D2_TRIP_MILES,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=D2_DROPOFF_ANCHOR,
        ),
        "match": dict(offer_id=7714, location_type="dropoff"),
        "expect_match_passes": True,
        "expected_held_by": None,
    },
    {
        "label": "2026-05-05 dentist drive Stop 2 (88s outbound dropoff, full trip)",
        "coords": (29.510316, -95.527151),
        "duration_s": 88,
        "speed_mph": 0.0,
        "cumulative_miles": DENT_DROPOFF_ANCHOR + DENT_TRIP_MILES,
        "offer": dict(
            id=8200, pickup_miles=DENT_PICKUP_MILES, trip_miles=DENT_TRIP_MILES,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=DENT_DROPOFF_ANCHOR,
        ),
        "match": dict(offer_id=8200, location_type="dropoff"),
        "expect_match_passes": True,
        "expected_held_by": None,
    },
    {
        "label": "Synthetic: brief 5s tap (Motion Gate held by duration)",
        "coords": (29.50, -95.50),
        "duration_s": 5,
        "speed_mph": 0.0,
        "cumulative_miles": D1_DROPOFF_ANCHOR + D1_TRIP_MILES,
        "offer": dict(
            id=9001, pickup_miles=D1_PICKUP_MILES, trip_miles=D1_TRIP_MILES,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=D1_DROPOFF_ANCHOR,
        ),
        "match": dict(offer_id=9001, location_type="dropoff"),
        "expect_match_passes": False,
        "expected_held_by": "motion (transient)",
    },
    {
        "label": "Synthetic: rolling 5mph (Motion Gate held by speed)",
        "coords": (29.50, -95.50),
        "duration_s": 30,
        "speed_mph": 5.0,
        "cumulative_miles": D1_DROPOFF_ANCHOR + D1_TRIP_MILES,
        "offer": dict(
            id=9002, pickup_miles=D1_PICKUP_MILES, trip_miles=D1_TRIP_MILES,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=D1_DROPOFF_ANCHOR,
        ),
        "match": dict(offer_id=9002, location_type="dropoff"),
        "expect_match_passes": False,
        "expected_held_by": "motion (moving)",
    },
]


def run_scenario(s):
    cluster = FakeCluster(
        duration_s=s["duration_s"],
        median_lat=s["coords"][0],
        median_lng=s["coords"][1],
    )
    offer = FakeOffer(**s["offer"])
    match = FakeMatch(**s["match"])

    verdict = evaluate_gates(
        cluster=cluster,
        cumulative_miles=s["cumulative_miles"],
        queue_offers=[offer],
        speed_mph=s["speed_mph"],
    )
    survivors = filter_matches_by_gates([match], verdict)

    actual_passes = len(survivors) > 0

    held_offers, held_legs = verdict.held_offer_ids_and_legs()
    leg_detail = verdict.leg_eligibility.get(str(offer.id))
    if leg_detail is not None:
        pickup_r = leg_detail.pickup
        dropoff_r = leg_detail.dropoff
        leg_str = (
            f"pickup[{pickup_r.reason}, p={_fmt_progress(pickup_r.progress)}] "
            f"dropoff[{dropoff_r.reason}, p={_fmt_progress(dropoff_r.progress)}]"
        )
    else:
        leg_str = "—"

    return {
        "label": s["label"],
        "expected": s["expect_match_passes"],
        "actual": actual_passes,
        "match_pass_correct": actual_passes == s["expect_match_passes"],
        "motion": verdict.motion_verdict,
        "leg_detail": leg_str,
        "held_offers": held_offers,
        "held_legs": held_legs,
        "expected_held_by": s["expected_held_by"],
        "cm": s["cumulative_miles"],
    }


def _fmt_progress(p):
    if p is None:
        return "—"
    return f"{p:.2f}"


def main():
    print()
    print("=" * 100)
    print("Motion Gate Sprint — kickoff scenario validation")
    print("=" * 100)
    print()

    results = [run_scenario(s) for s in SCENARIOS]

    pass_count = sum(1 for r in results if r["match_pass_correct"])
    fail_count = len(results) - pass_count

    for r in results:
        marker = "PASS" if r["match_pass_correct"] else "FAIL"
        print(f"[{marker}] {r['label']}")
        print(f"        cm={r['cm']:.2f}  motion={r['motion']:<10}  "
              f"match_passes_gate={r['actual']:<5}  expected={r['expected']}")
        print(f"        legs:   {r['leg_detail']}")
        if r["expected_held_by"]:
            print(f"        expected hold reason: {r['expected_held_by']}")
            print(f"        actual: held_offers={r['held_offers']} held_legs={r['held_legs']}")
        print()

    print("=" * 100)
    print(f"Summary: {pass_count}/{len(results)} scenarios pass")
    print("=" * 100)
    print()

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
