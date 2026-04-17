"""
state_machine.py — Single interface to driver state.

Golden Rule: Postgres owns Truth. Python owns Strategy.

READ:       DriverStateMachine.read(driver_id, cur)
WRITE:      DriverStateMachine.transition(driver_id, trigger, cur, conn, **coords)

No other file may write to driver_trip_state.
"""

import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

_STATE_EMOJI = {
    'UNCOMMITTED': '🟢',
    'ENROUTE':     '🟡',
    'IN_TRIP':     '🔵',
    'STACKED':     '🟠',
}

_TRIGGER_LABELS = {
    'offer_accepted':           'Offer accepted',
    'offer_declined':           'Ride cancelled',
    'offer_cancelled_implicit': 'Ride cancelled',
    'pickup_confirmed':         'Pickup Nail It',
    'gps_convergence':          'GPS convergence',
    'dropoff_confirmed':        'Dropoff Nail It',
    'manual_reset':             'Manual reset',
    'watchdog_auto_reset':      'Watchdog auto-reset',
    's11_driver_override':      'S11 override',
    'gps_divergence':           'GPS divergence',
    'replay_harness':           'Replay harness',
}


def _notify_discord(from_state: str, to_state: str, trigger: str) -> None:
    webhook_url = os.environ.get('DISCORD_WEBHOOK_URL')
    if not webhook_url:
        return
    f_e = _STATE_EMOJI.get(from_state, '⚪')
    t_e = _STATE_EMOJI.get(to_state, '⚪')
    label = _TRIGGER_LABELS.get(trigger, trigger)
    msg = f"{f_e} **{from_state}** → {t_e} **{to_state}** — {label}"
    try:
        data = json.dumps({'content': msg}).encode()
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={'Content-Type': 'application/json',
                     'User-Agent': 'PuddleJumper-StateMachine/1.0'},
            method='POST'
        )
        urllib.request.urlopen(req, timeout=3)
        logger.info(f"📣 Discord notified: {from_state} → {to_state} ({trigger})")
    except Exception as e:
        logger.warning(f"⚠️ Discord notify failed (non-fatal): {e}")


def _default_state() -> dict:
    return {
        'state': 'UNCOMMITTED',
        'current_offer_id': None,
        'pickup_lat': None, 'pickup_lng': None, 'pickup_h3': None,
        'dropoff_lat': None, 'dropoff_lng': None, 'dropoff_h3': None,
        'nailed_pickup_lat': None, 'nailed_pickup_lng': None, 'nailed_pickup_error_m': None,
        'nailed_dropoff_lat': None, 'nailed_dropoff_lng': None, 'nailed_dropoff_error_m': None,
        'state_updated_at': None,
        'heartbeat': None, 'heartbeat_at': None,
        'arc_center_lat': None, 'arc_center_lng': None,
    }


from nail_it_core import clear_buffer as _clear_stop_buffer

class DriverStateMachine:

    @staticmethod
    def read(driver_id: str, cur) -> dict:
        """
        Pure read. Calls sm_read(). Never writes anything.
        Returns _default_state() if driver row not found.
        """
        try:
            cur.execute(
                "SELECT * FROM app_private.sm_read(%s)",
                (driver_id,)
            )
            row = cur.fetchone()
            if row is None:
                logger.warning(f"[SM] sm_read: no row for driver {driver_id}")
                return _default_state()
            return dict(row)
        except Exception as e:
            logger.error(f"[SM] sm_read failed for {driver_id}: {e}")
            return _default_state()

    @staticmethod
    def transition(
        driver_id: str,
        trigger: str,
        cur,
        conn,
        **coords
    ) -> dict:
        """
        THE ONLY WAY TO CHANGE DRIVER STATE.

        Calls sm_transition(), commits, fires Discord on success.
        Never raises — returns {'success': False, 'error': ...} on failure.

        Accepted coord kwargs:
            pickup_lat, pickup_lng, pickup_h3
            dropoff_lat, dropoff_lng, dropoff_h3
            nailed_pickup_lat, nailed_pickup_lng, nailed_pickup_error_m
            nailed_dropoff_lat, nailed_dropoff_lng, nailed_dropoff_error_m
            offer_id, clear_coords (bool)
        """
        try:
            cur.execute("""
                SELECT * FROM app_private.sm_transition(
                    %s, %s,
                    %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s
                )
            """, (
                driver_id, trigger,
                coords.get('offer_id'),
                coords.get('pickup_lat'),
                coords.get('pickup_lng'),
                coords.get('pickup_h3'),
                coords.get('dropoff_lat'),
                coords.get('dropoff_lng'),
                coords.get('dropoff_h3'),
                coords.get('nailed_pickup_lat'),
                coords.get('nailed_pickup_lng'),
                coords.get('nailed_pickup_error_m'),
                coords.get('nailed_dropoff_lat'),
                coords.get('nailed_dropoff_lng'),
                coords.get('nailed_dropoff_error_m'),
                coords.get('clear_coords', False),
            ))
            result = dict(cur.fetchone())
            # Normalize 'message' → 'error' for backward compat
            if 'message' in result and 'error' not in result:
                result['error'] = result['message']
            conn.commit()

            if result['success']:
                # ── Defensive stop-buffer clear on trip-cycle boundaries ──
                # Fires on entry to UNCOMMITTED (trip end / cancel / watchdog
                # reset / manual reset) and exit from UNCOMMITTED (offer
                # accepted). Idempotent — second call is a no-op. Does NOT
                # fire mid-trip (IN_TRIP/STACKED/REFINE_DROPOFF transitions
                # that don't touch UNCOMMITTED are untouched).
                if 'UNCOMMITTED' in (result.get('from_state'), result.get('to_state')):
                    try:
                        _old_size = _clear_stop_buffer(driver_id)
                        if _old_size > 0:
                            logger.info(
                                f"[BUFFER] Cleared on UNCOMMITTED for driver "
                                f"{driver_id} — {_old_size} stops discarded"
                            )
                    except Exception as _e:
                        logger.warning(
                            f"[BUFFER] Clear failed (non-fatal) during "
                            f"{result.get('from_state')}->{result.get('to_state')}: {_e}"
                        )

                _notify_discord(result['from_state'], result['to_state'], trigger)
            else:
                logger.warning(
                    f"[SM] Transition rejected: {trigger} "
                    f"({result.get('from_state')} → {result.get('error')})"
                )

            return result

        except Exception as e:
            logger.error(f"[SM] Transition exception [{trigger}] for {driver_id}: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            return {'success': False, 'from_state': None, 'to_state': None, 'error': str(e)}
