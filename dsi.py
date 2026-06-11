"""Canonical DSI (Drive Score Index) constants + compute helper.

DSI is a READ-ONLY / observational economic signal as of step 1
(Gemini-ratified 2026-06-02). It MUST NOT feed any Accept/Decline verdict —
the writers only populate the versioned ``dsi_v1`` column on new offer rows.

Versioned (``dsi_v1``, not flat ``dsi``) because the formula has two volatile
inputs: the annually-revised IRS mileage rate and a calibratable mile weight.
A change to either is a new version (dsi_v2, ...), which preserves multi-year
backtest integrity — a flat column would silently become a mixed dataset.

Lives at repo root next to the other engine-constant modules (pudo_types,
tad, driver_queue) so both writers import it the same way:
``from dsi import compute_dsi_v1``.
"""

# IRS standard mileage rate ($/mi). VOLATILE: revised annually by the IRS.
IRS_RATE_PER_MILE = 0.725

# Weight applied to the per-mile delta. VOLATILE: weight-optimization
# (phase 3) may retune it. A change here bumps the column version.
DSI_MILE_WEIGHT = 12


def compute_dsi_v1(effective_hourly_rate, dollars_per_mile):
    """Return the dsi_v1 value, or None (NULL-strict).

        dsi_v1 = effective_hourly_rate
                 + DSI_MILE_WEIGHT * (dollars_per_mile - IRS_RATE_PER_MILE)

    Both inputs are Uber's displayed card values already on the row — NOT
    recomputed from fare/time.

    NULL-STRICT (Gemini invariant): if EITHER input is None, return None.
    Never compute a partial value, never substitute 0 — a zeroed/guessed DSI
    is a toxic point that poisons the IDW surface for neighboring hexes.
    """
    if effective_hourly_rate is None or dollars_per_mile is None:
        return None
    return effective_hourly_rate + DSI_MILE_WEIGHT * (dollars_per_mile - IRS_RATE_PER_MILE)


def compute_personal_dsi(effective_hourly_rate, dollars_per_mile, driver_cost_per_mile):
    """Return the driver's PERSONAL DSI, or None (NULL-strict).

        personal_dsi = effective_hourly_rate
                       + DSI_MILE_WEIGHT * (dollars_per_mile - driver_cost_per_mile)

    Identical to compute_dsi_v1 except the standardization anchor (IRS_RATE_PER_MILE)
    is replaced by the driver's ACTUAL cost_per_mile (from their settings). Equivalent
    to ``compute_dsi_v1(...) + DSI_MILE_WEIGHT * (IRS_RATE_PER_MILE - driver_cost_per_mile)``
    — the spec §3 Personal-DSI delta. A driver whose real cost is below the IRS anchor
    scores every offer higher than Standard DSI: their efficiency edge, and exactly why
    the decision (spec §6) compares Personal DSI to the Standard-anchored Local Market DSI.

    Used ONLY at decision time. The stored, cross-driver-comparable surface stays Standard
    (compute_dsi_v1); never write personal DSI to community_offers/offer_history.

    NULL-STRICT: any None input (including a missing cost_per_mile) returns None — the
    caller must fall back rather than guess (a fabricated cost poisons the verdict).
    """
    if (effective_hourly_rate is None or dollars_per_mile is None
            or driver_cost_per_mile is None):
        return None
    return effective_hourly_rate + DSI_MILE_WEIGHT * (dollars_per_mile - driver_cost_per_mile)
