"""offer_copies.py -- SQL filter that drops re-logged copies of the same offer.

The same offer card is sometimes captured and logged more than once: re-captures
while it is still on screen, and testing. Every copy gets its own row and id, so
market aggregates (DSI heatmap, time grid) counted it two or three times. Measured
2026-09-14: 79 of 1,109 community_offers rows in 120 days were copies.

A row is a copy of an EARLIER row with the same fare and trip miles when either
  * trip minutes and pickup miles are also identical, within an hour, or
  * it is within 3 minutes and trip minutes differ by no more than 2
    (pickup miles and minutes move as the car moves between captures).
Identical cheap fares days apart are genuinely different trips, so the windows are
short. decisions/bar_tuner.py applies the same rule to one driver's decision_log.
"""


def not_a_copy(alias: str, table: str) -> str:
    """AND-clause for a query over `table AS alias` that keeps only first copies."""
    return f"""
      AND NOT EXISTS (
        SELECT 1 FROM {table} e
        WHERE (e.created_at, e.id) < ({alias}.created_at, {alias}.id)
          AND e.created_at >= {alias}.created_at - interval '1 hour'
          AND e.fare = {alias}.fare
          AND e.trip_miles IS NOT DISTINCT FROM {alias}.trip_miles
          AND (   (e.trip_minutes IS NOT DISTINCT FROM {alias}.trip_minutes
                   AND e.pickup_miles IS NOT DISTINCT FROM {alias}.pickup_miles)
               OR (e.created_at >= {alias}.created_at - interval '3 minutes'
                   AND abs(coalesce(e.trip_minutes, 0) - coalesce({alias}.trip_minutes, 0)) <= 2)))
    """
