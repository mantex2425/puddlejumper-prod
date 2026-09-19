"""
Account Management Blueprint
Handles account deletion with GDPR-compliant data anonymization
"""

import logging
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

account_bp = Blueprint('account', __name__)


@account_bp.route('/delete-account', methods=['DELETE'])
def delete_account():
    """
    Permanently delete user account.
    
    Privacy-compliant process:
    1. Anonymizes decision_log (SET driver_id = NULL) - preserves community data
    2. Deletes driver_settings_new (personal preferences)
    3. Deletes Firebase auth account
    
    Returns:
        JSON with deletion summary
    """
    # Lazy load dependencies
    from db import get_db
    from utils import verify_and_get_user_id
    
    try:
        # Get authenticated user
        driver_id = verify_and_get_user_id(request)
        
        if not driver_id:
            return jsonify({'error': 'No driver ID provided'}), 401
        
        logger.info(f"Account deletion requested for driver: {driver_id[:8]}...")
        
        conn = get_db()
        try:
            with conn.cursor() as cur:
                # Step 1: Anonymize ride data (preserve community intelligence)
                cur.execute("""
                    UPDATE app_private.decision_log 
                    SET driver_id = NULL 
                    WHERE driver_id = %s
                """, (driver_id,))
                rides_anonymized = cur.rowcount
                logger.info(f"Anonymized {rides_anonymized} ride records")
                
                # Step 2: Delete personal settings
                cur.execute("""
                    DELETE FROM app_private.driver_settings_new 
                    WHERE driver_id = %s
                """, (driver_id,))
                settings_deleted = cur.rowcount
                logger.info(f"Deleted {settings_deleted} settings record(s)")

                # Step 2b: every other table that names this driver (2026-09-19).
                purged = _purge_driver_rows(cur, driver_id)
                logger.info(f"Purged driver rows: {purged or 'none'}")

                conn.commit()
                
        finally:
            conn.close()
        
        # Step 3: Delete Firebase auth account
        firebase_deleted = False
        try:
            from firebase_admin import auth
            auth.delete_user(driver_id)
            firebase_deleted = True
            logger.info(f"Firebase auth account deleted")
        except Exception as firebase_error:
            # Log but don't fail - database is already cleaned
            logger.warning(f"Firebase deletion warning: {firebase_error}")
        
        return jsonify({
            'success': True,
            'message': 'Account permanently deleted',
            'details': {
                'rides_anonymized': rides_anonymized,
                'settings_deleted': settings_deleted,
                'rows_purged': purged,
                'auth_deleted': firebase_deleted
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Account deletion error: {str(e)}")
        return jsonify({'error': str(e)}), 500



# Every table that carries a driver's identifier, and what deletion does with it.
#
# Until 2026-09-19 deletion touched two of these: decision_log (de-identified) and
# driver_settings_new (deleted). The driver's Firebase UID stayed behind in every other
# table -- a survey that day found twelve, including 794k location-bearing heartbeat rows.
# The dead ones were dropped; these are what remain, and the sweep below clears them all.
#
# decision_log is DE-IDENTIFIED, not deleted: the offer economics stay in the shared market
# data with nothing tying them to a person, exactly as the privacy policy describes. Add any
# new table with a driver column to this list, or deletion silently stops being complete.
_PURGE_TABLES = (
    ("app_private.crash_reports", "driver_id"),
    ("app_private.driver_active_market", "driver_id"),
    ("app_private.driver_trip_state", "driver_id"),
    ("public.driver_markets", "driver_id"),
    ("public.markets", "driver_id"),
    ("public.referral_codes", "user_id"),
)


def _purge_driver_rows(cur, driver_id):
    """Delete every row keyed to this driver. Returns {table: rows deleted}."""
    deleted = {}
    for table, column in _PURGE_TABLES:
        cur.execute(f"DELETE FROM {table} WHERE {column} = %s", (driver_id,))
        if cur.rowcount:
            deleted[table] = cur.rowcount
    return deleted


@account_bp.route('/delete-account', methods=['GET'])
def delete_account_info():
    """
    Returns information about what will be deleted.
    Useful for confirmation dialog in the app.
    """
    from db import get_db
    from utils import verify_and_get_user_id
    
    try:
        driver_id = verify_and_get_user_id(request)
        
        conn = get_db()
        try:
            with conn.cursor() as cur:
                # Count rides that will be anonymized
                cur.execute("""
                    SELECT COUNT(*) 
                    FROM app_private.decision_log 
                    WHERE driver_id = %s
                """, (driver_id,))
                ride_count = cur.fetchone()[0]
                
                # Check if settings exist
                cur.execute("""
                    SELECT EXISTS(
                        SELECT 1 FROM app_private.driver_settings_new 
                        WHERE driver_id = %s
                    )
                """, (driver_id,))
                has_settings = cur.fetchone()[0]
                
        finally:
            conn.close()
        
        return jsonify({
            'driver_id': driver_id[:8] + '...',  # Partial for privacy
            'data_to_be_affected': {
                'ride_records': ride_count,
                'ride_action': 'anonymized (community data preserved)',
                'settings': 'deleted' if has_settings else 'none found',
                'auth_account': 'deleted'
            },
            'warning': 'This action is permanent and cannot be undone.',
            'privacy_note': 'Anonymized ride data (location, time, rates) remains in the community pool but can never be traced back to you.'
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@account_bp.route('/request-deletion', methods=['POST'])
def request_deletion():
    """
    Public endpoint — no auth required.
    For users who cannot log in but want to request account deletion.
    Inserts email into app_private.deletion_requests for manual processing.
    """
    from db import get_db

    data = request.get_json() or {}
    email = data.get('email', '').strip().lower()

    if not email or '@' not in email:
        return jsonify({'success': False, 'error': 'Valid email required'}), 400

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_private.deletion_requests (email) VALUES (%s)",
                (email,)
            )
        conn.commit()
        return jsonify({'success': True, 'message': 'Deletion request received'})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()
