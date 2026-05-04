"""
driver_debug.py — PuddleJumper Debug Routes
Internal-only endpoints for validating in-memory state.
No auth required — internal use only via X-Internal-Replay header.

2026-05-04 Commit 3b: get_buffer_status route removed alongside
get_buffer (deleted from nail_it_core). Blueprint preserved as a
stub so app.py registration sites do not break; future debug
endpoints can be added here without re-wiring imports.
"""
from flask import Blueprint

driver_debug_bp = Blueprint('driver_debug', __name__)