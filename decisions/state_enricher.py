import logging
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from state_machine import DriverStateMachine



# ======================================================================
# PIPELINE STAGE 4 — enrich_with_state
# ======================================================================
def enrich_with_state(cur, conn, uid, ep, result):
    """
    get_driver_state + S04 implicit cancel if ENROUTE.
    Never raises. Returns (result, driver_state dict).
    result["driverState"] is always set before return.
    """
    default_state = {
        "state":          "UNCOMMITTED",
        "arc_center_lat": ep["current_lat"] or 0,
        "arc_center_lng": ep["current_lng"] or 0,
        "converged":      False,
    }

    if not (ep["current_lat"] and ep["current_lng"]):
        result["driverState"] = "UNCOMMITTED"
        return result, default_state

    try:
        driver_state = DriverStateMachine.read(uid, cur)
        if driver_state is None:
            logging.warning("[STATE] DriverStateMachine.read returned None — using default state")
            result["driverState"] = "UNCOMMITTED"
            return result, default_state
        logging.info(
            f"[STATE] Driver state: {driver_state['state']} "
            f"(arc: {driver_state['arc_center_lat']:.4f}, "
            f"{driver_state['arc_center_lng']:.4f})"
        )

        # ── S04: ENROUTE + new offer = implicit cancel ────────────────
        _s04_is_retry = False
        if driver_state["state"] == "ENROUTE" and ep["p_lat"] and ep["p_lng"]:
            try:
                cur.execute("""
                    SELECT pickup_lat, pickup_lng
                    FROM app_private.driver_trip_state
                    WHERE driver_id = %s AND pickup_lat IS NOT NULL
                """, (uid,))
                _active = cur.fetchone()
                if _active:
                    _dlat = float(_active["pickup_lat"]) - ep["p_lat"]
                    _dlng = float(_active["pickup_lng"]) - ep["p_lng"]
                    _dist = (_dlat**2 + _dlng**2) ** 0.5 * 69.0
                    if _dist < 0.1:
                        _s04_is_retry = True
                        logging.info(
                            f"[S04] Suppressed -- retry detected (dist={_dist:.3f}mi)"
                        )
            except Exception as _e:
                logging.warning(f"[WARN] S04 retry check failed: {_e}")

        if driver_state["state"] == "ENROUTE" and not _s04_is_retry:
            logging.info("[S04] New offer while ENROUTE -> implicit cancel -> UNCOMMITTED")
            try:
                cur.execute("""
                    UPDATE app_private.pickup_market_signals
                    SET offer_status = 'cancelled'
                    WHERE offer_id = (
                        SELECT current_offer_id::integer
                        FROM app_private.driver_trip_state
                        WHERE driver_id = %s
                    ) AND offer_status = 'pending'
                """, (uid,))
                DriverStateMachine.transition(
                    uid, 'offer_cancelled_implicit', cur, conn,
                    clear_coords=True
                )
                driver_state = DriverStateMachine.read(uid, cur)
                logging.info("[S04] Complete -- UNCOMMITTED")
            except Exception as s04_err:
                logging.warning(f"[WARN] S04 failed: {s04_err}")
                try:    conn.rollback()
                except: pass

        result["driverState"] = driver_state["state"]
        return result, driver_state

    except Exception as ds_err:
        logging.error(f"[ERROR] enrich_with_state failed: {ds_err}", exc_info=True)
        try:    conn.rollback()
        except: pass
        result["driverState"] = "UNCOMMITTED"
        return result, default_state

