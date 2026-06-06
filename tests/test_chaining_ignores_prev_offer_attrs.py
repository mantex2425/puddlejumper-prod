"""Guard-test for the Class B self-proxy invariant (Gemini 00648 Call A condition).

Class B recompute calls compute_offer_expectations(d, prev_offer=d, **kwargs),
passing the deferred offer as its OWN prev_offer purely to satisfy the
`if prev_offer is not None:` presence guard. This is safe ONLY because the
chaining branch reads execution metrics EXCLUSIVELY from the prev_expected_*
kwargs, never from prev_offer.<attr>.

This test PINS that invariant executably (stronger than a comment): it passes a
DECOY prev_offer whose attributes, if read, would produce a different/wrong
answer — and asserts the result still equals the kwarg-derived chained anchor.
If a future edit makes the chaining branch read prev_offer.<attr>, this test
goes RED immediately — the 9132-class confident-wrong hazard, tripwired.

Pure unit test (no DB): compute_offer_expectations is pure arithmetic.
"""
import datetime

from tad import compute_offer_expectations
from pudo_types import Offer, TargetSpec

UTC = datetime.timezone.utc


def _offer(offer_id, pickup_miles, trip_miles, pickup_minutes, trip_minutes):
    return Offer(
        offer_id=offer_id,
        accepted_at=datetime.datetime.now(UTC),
        pickup=TargetSpec(lat=0.0, lng=0.0, address_class="poi", named_roads=()),
        dropoff=TargetSpec(lat=0.0, lng=0.0, address_class="poi", named_roads=()),
        pickup_miles=pickup_miles,
        trip_miles=trip_miles,
        pickup_minutes=pickup_minutes,
        trip_minutes=trip_minutes,
    )


def test_chaining_ignores_prev_offer_attrs():
    """The stacked-chaining branch must use prev_expected_* kwargs, NOT prev_offer.attrs.

    Setup: D is the offer being (re)computed. The DECOY prev_offer carries
    deliberately wrong miles/minutes — if the chaining branch ever reads them
    instead of the kwargs, the anchor would come out wrong and this fails.
    """
    now = datetime.datetime.now(UTC)

    # The offer being recomputed: pickup_miles drives the chained pickup anchor.
    d = _offer("D", pickup_miles=3.0, trip_miles=7.0, pickup_minutes=10, trip_minutes=20)

    # DECOY prev_offer: if the body (wrongly) read prev_offer.pickup_miles /
    # .trip_miles / .expected_* off this object, the answer would diverge from
    # the kwarg-derived one. We make its values absurd so divergence is obvious.
    decoy = _offer("DECOY", pickup_miles=999.0, trip_miles=999.0,
                   pickup_minutes=999, trip_minutes=999)

    # The TRUE chaining anchors arrive via kwargs (X's dropoff anchor).
    prev_dropoff_dist = 120.0
    prev_dropoff_eta = now - datetime.timedelta(minutes=5)

    exp = compute_offer_expectations(
        d,
        prev_offer=decoy,                       # presence-flag only; attrs must be IGNORED
        current_odometer=125.0,
        now=now,
        prev_expected_dropoff_arrival_time=prev_dropoff_eta,
        prev_expected_dropoff_distance=prev_dropoff_dist,
    )

    assert exp is not None, "chaining branch must fire (prev_offer present)"

    # Kwarg-derived chained anchor: prev_dropoff_dist + D.pickup_miles = 123.0.
    # If the body read decoy.pickup_miles (999), this would be ~1119 — RED.
    expected_pickup = prev_dropoff_dist + d.pickup_miles  # 123.0
    assert abs(exp.expected_pickup_distance - expected_pickup) < 0.001, (
        f"chaining read the WRONG source: expected {expected_pickup} from kwargs "
        f"+ D.pickup_miles, got {exp.expected_pickup_distance}. If this is ~1119, "
        f"the body is reading prev_offer.pickup_miles (the decoy) — INVARIANT BROKEN."
    )

    # Dropoff anchor chains off pickup + D.trip_miles = 130.0 (not decoy's 999).
    expected_dropoff = expected_pickup + d.trip_miles  # 130.0
    assert abs(exp.expected_dropoff_distance - expected_dropoff) < 0.001, (
        f"dropoff anchor must derive from D.trip_miles, not decoy: "
        f"expected {expected_dropoff}, got {exp.expected_dropoff_distance}"
    )


def test_self_proxy_equals_decoy_proxy():
    """prev_offer=d (the production self-proxy) must equal prev_offer=decoy.

    Proves prev_offer's identity is irrelevant to the math — only the kwargs
    matter. The production hack (prev_offer=d) is therefore safe.
    """
    now = datetime.datetime.now(UTC)
    d = _offer("D", pickup_miles=3.0, trip_miles=7.0, pickup_minutes=10, trip_minutes=20)
    decoy = _offer("DECOY", pickup_miles=999.0, trip_miles=999.0,
                   pickup_minutes=999, trip_minutes=999)
    kw = dict(
        current_odometer=125.0, now=now,
        prev_expected_dropoff_arrival_time=now - datetime.timedelta(minutes=5),
        prev_expected_dropoff_distance=120.0,
    )
    via_self = compute_offer_expectations(d, prev_offer=d, **kw)
    via_decoy = compute_offer_expectations(d, prev_offer=decoy, **kw)
    assert via_self is not None and via_decoy is not None
    assert abs(via_self.expected_pickup_distance - via_decoy.expected_pickup_distance) < 0.001
    assert abs(via_self.expected_dropoff_distance - via_decoy.expected_dropoff_distance) < 0.001
