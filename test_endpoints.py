import sys
import json
import logging
from unittest.mock import MagicMock, patch

# ── 1. AGGRESSIVE AUTO-MOCKING ───────────────────────────────────────────────
class _AutoMockFinder:
    _PREFIXES = (
        "firebase_admin", "flask_cors", "anthropic", "redis", "h3",
        "google", "langchain", "langchain_core", "langchain_google_genai",
        "langchain_google_vertexai", "langgraph", "vertexai",
        "geopy", "requests", "numpy", "pandas", "openai", "jwt",
        "psycopg", "psycopg_pool",
    )
    def find_module(self, name, path=None):
        if any(name == p or name.startswith(p + ".") for p in self._PREFIXES):
            return self
        return None
    def load_module(self, name):
        if name not in sys.modules:
            m = MagicMock()
            m.__path__ = []
            m.__package__ = name
            sys.modules[name] = m
        return sys.modules[name]

sys.meta_path.insert(0, _AutoMockFinder())
logging.basicConfig(level=logging.ERROR)

# ── 2. SMART DB MOCK ────────────────────────────────────────────────────────
def make_mock_db():
    cur = MagicMock()
    mock_row = {
        # identity
        "id": 1843, "pms_id": 999, "offer_id": 1843,
        "current_offer_id": 1843,
        # state
        "state": "UNCOMMITTED", "dist_miles": 1.2,
        # pickup
        "pickup_lat": 29.5508, "pickup_lng": -95.5233,
        "pickup_h3": "88446ca839fffff",
        "actual_pickup_lat": None, "actual_pickup_lng": None,
        "triangulation_error_m": 250.0,
        # dropoff
        "dropoff_lat": 29.9902, "dropoff_lng": -95.3368,
        "dropoff_h3": "88446ca8a5fffff",
        "nailed_pickup_lat": None, "nailed_pickup_lng": None,
        # trip
        "trip_miles": 21.8, "trip_minutes": 34,
        "pickup_miles": 4.62, "pickup_minutes": 14,
        "deadhead_miles": 4.62, "deadhead_minutes": 14,
        # arc banding
        "arc_center_lat": 29.5508, "arc_center_lng": -95.5233,
        "hex": "88446ca839fffff",
        # decision
        "verdict": "ACCEPT", "reason": "Smoke Test",
        "net_pay": 19.05, "hourly_rate": 32.50, "dollars_per_mile": 0.87,
        "threshold_source": "mock", "trace_data": {},
        # market
        "rate_per_mile": 0.87, "rate_per_hour": 32.50,
        "min_rate_per_mile": 0.70, "min_rate_per_hour": 25.0,
        "market_id": "6a35d28b-8e6c-4d60-94aa-2661e2650863",
        # nearby offer (heartbeat)
        "nearby_offer_id": None,
        # pickup confirm
        "existing_error_m": 250.0,
        # dropoff confirm
        "actual_dropoff_lat": None, "actual_dropoff_lng": None,
        "actual_dropoff_h3": None, "actual_dropoff_at": None,
        "dropoff_error_m": None, "dropoff_classification": None,
        "nailed_dropoff_lat": None, "nailed_dropoff_lng": None,
        # decisions engine
        "deadhead_cost": 1.85,
        "jsonb_array_elements_text": None,
        "success": True,
        # settings
        "cost_per_mile": 0.67, "cost_per_hour": 12.0,
        "revenue_per_mile": 1.00, "revenue_per_hour": 10.0,
        "min_effective_hourly_rate": 15.0,
        "min_effective_dollar_per_mile": 0.70,
        "deadhead_percent": 1.0, "deadhead_basis": "hourly",
        "max_pickup_miles": 25.0,
        "settings": {},
        # nail_it_core compute_error
        "actual_h3": "88446ca839fffff",
        "error_m": 250.0,
        # state machine
        "from_state": "UNCOMMITTED",
        "to_state": "ENROUTE",
        # decisions engine
        "arrival_detected": False,
        # nail_it_core get_accuracy_stats
        "total_confirmed": 0, "avg_error_m": 0.0, "best_error_m": 0.0,
        # decisions engine
        "switch_to_mode": None,
        "switch_to_market_id": None,
        "pct_on_target": 0.0,
    }
    cur.fetchone.return_value = mock_row
    cur.fetchall.return_value = [mock_row]
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur

# ── 3. TEST HARNESS ──────────────────────────────────────────────────────────
PASS = "✅ PASS"
FAIL = "❌ FAIL"
results = []

def run_test(name, fn):
    try:
        fn()
        results.append((PASS, name))
    except Exception as e:
        results.append((FAIL, f"{name}: {type(e).__name__}: {e}"))

MOCK_DRIVER_ID = "UjT1hE9eBXh2q95aSZYOkzDJ8lo1"

# We mock get_db and auth globally to bypass network/auth requirements
with patch("utils.verify_and_get_user_id", return_value=MOCK_DRIVER_ID), \
     patch("db.get_db") as mock_get_db:

    mock_conn, mock_cur = make_mock_db()
    mock_get_db.return_value = mock_conn

    # Import app AFTER patches are applied
    from app import app
    app.config["TESTING"] = True
    client = app.test_client()

    def post(url, body):
        return client.post(url, data=json.dumps(body), 
                           content_type="application/json",
                           headers={"Authorization": "Bearer smoke-test"})

    def test_health():
        r = client.get("/api/v1/health")
        assert r.status_code == 200, f"Got {r.status_code}: {r.data.decode()}"

    def test_dropoff_confirm():
        r = post("/api/v1/dropoff/confirm", {"lat": 29.99, "lng": -95.33})
        assert r.status_code != 500, r.data.decode()

    def test_heartbeat():
        r = post("/api/v1/driver/heartbeat", {"lat": 29.55, "lng": -95.52, "speed_mph": 0})
        assert r.status_code != 500, r.data.decode()

    def test_reset():
        r = post("/api/v1/driver/reset", {})
        assert r.status_code != 500

    def test_decisions():
        payload = {
            "fare": 19.05, "tripMiles": 21.8, "tripMinutes": 34,
            "pickupMinutes": 14, "pickupMiles": 4.62,
            "lat": 29.5508709, "lng": -95.52336609999999,
            "dropoffLat": 29.9902, "dropoffLng": -95.3368,
            "currentLat": 29.5071104, "currentLng": -95.5026206,
            "marketId": "6a35d28b-8e6c-4d60-94aa-2661e2650863",
            "isPuddleJumpMode": False
        }
        r = post("/api/v1/decisions/", payload)
        assert r.status_code != 500, r.data.decode()

    run_test("GET  /api/v1/health", test_health)
    run_test("POST /api/v1/dropoff/confirm", test_dropoff_confirm)
    run_test("POST /api/v1/driver/heartbeat", test_heartbeat)
    run_test("POST /api/v1/driver/reset", test_reset)
    run_test("POST /api/v1/decisions/", test_decisions)

# ── 4. OUTPUT ────────────────────────────────────────────────────────────────
print("\n" + "="*60 + "\n  Endpoint Smoke Tests\n" + "="*60)
for status, name in results:
    print(f"  {status}  {name}")
print("-" * 60)
passed = sum(1 for r in results if r[0] == PASS)
print(f"  {passed}/{len(results)} passing\n" + "="*60 + "\n")
if passed < len(results): sys.exit(1)
