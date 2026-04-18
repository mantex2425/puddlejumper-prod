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
        _arc_lat = driver_state.get('arc_center_lat')
        _arc_lng = driver_state.get('arc_center_lng')
        if _arc_lat is not None and _arc_lng is not None:
            _arc_str = f"(arc: {_arc_lat:.4f}, {_arc_lng:.4f})"
        else:
            _arc_str = "(no arc)"
        logging.info(f"[STATE] Driver state: {driver_state['state']} {_arc_str}")

        # ── S04: ENROUTE + new offer = implicit cancel ────────────────
        # A new offer card from Uber = the previous transaction is dead.
        # No distance checks, no retry detection. We reset to a clean UNCOMMITTED state.
        # This prevents "ghost ENROUTE" states with stale current_offer_id.
        if driver_state["state"] == "ENROUTE":
            logging.info("[S04] New offer while ENROUTE → implicit cancel → UNCOMMITTED")
            try:
                # Mark the previous pending offer as cancelled
                cur.execute("""
                    UPDATE app_private.pickup_market_signals
                    SET offer_status = 'cancelled'
                    WHERE offer_id = (
                        SELECT current_offer_id::integer
                        FROM app_private.driver_trip_state
                        WHERE driver_id = %s
                    ) AND offer_status = 'pending'
                """, (uid,))
                # Reset state cleanly
                DriverStateMachine.transition(
                    uid, 'offer_cancelled_implicit', cur, conn,
                    clear_coords=True
                )
                driver_state = DriverStateMachine.read(uid, cur)
                logging.info("[S04] Complete — now UNCOMMITTED")
            except Exception as s04_err:
                logging.warning(f"[WARN] S04 failed: {s04_err}")
                try:
                    conn.rollback()
                except:
                    pass

        result["driverState"] = driver_state["state"]
        return result, driver_state

    except Exception as ds_err:
        logging.error(f"[ERROR] enrich_with_state failed: {ds_err}", exc_info=True)
        try:    conn.rollback()
        except: pass
        result["driverState"] = "UNCOMMITTED"
        return result, default_state

