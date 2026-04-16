"""
driver_debug.py — PuddleJumper Debug Routes
Internal-only endpoints for validating in-memory state.
No auth required — internal use only via X-Internal-Replay header.
"""
import time
from flask import Blueprint, request, jsonify
from nail_it_core import get_buffer

driver_debug_bp = Blueprint('driver_debug', __name__)

@driver_debug_bp.route('/driver/debug/buffer', methods=['GET'])
def get_buffer_status():
    """
    Returns current stop buffer stats for a driver.
    DIAGNOSE box read — no writes, no state changes.
    
    Usage: GET /api/v1/driver/debug/buffer
    Headers: X-Driver-Id: <driver_id>
    """
    replay = request.headers.get('X-Internal-Replay', '')
    if replay != 'puddlejumper-replay-2026':
        return jsonify({'error': 'unauthorized'}), 401

    driver_id = request.headers.get('X-Driver-Id')
    if not driver_id:
        return jsonify({'error': 'X-Driver-Id header required'}), 400

    buffer = get_buffer(driver_id)
    if not buffer:
        return jsonify({
            'driver_id': driver_id,
            'buffer_size': 0,
            'oldest_ts': None,
            'newest_ts': None,
            'last_speed_mph': None,
            'last_heading': None,
            'span_seconds': 0,
            'status': 'empty'
        })

    now = time.time()
    oldest = buffer[0]
    newest = buffer[-1]
    low_speed_points = sum(1 for p in buffer if p['speed_mph'] < 5.0)

    return jsonify({
        'driver_id':        driver_id,
        'buffer_size':      len(buffer),
        'oldest_ts':        oldest['ts'],
        'newest_ts':        newest['ts'],
        'span_seconds':     round(newest['ts'] - oldest['ts'], 1),
        'last_speed_mph':   round(newest['speed_mph'], 2),
        'last_heading':     newest.get('heading'),
        'low_speed_points': low_speed_points,
        'status':           'healthy'
    })
