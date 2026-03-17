import os
import re
import json
import logging
from flask import Blueprint, request, jsonify
from utils import verify_and_get_user_id

logger = logging.getLogger(__name__)

market_intelligence_bp = Blueprint('market_intelligence', __name__)

SYSTEM_PROMPT = """You are a market intelligence assistant for PuddleJumper, helping Uber drivers understand their market and improve their earnings. You have read-only access to anonymized rideshare market data.

## YOUR ROLE
Answer driver questions about market trends, earnings patterns, zone performance, and timing using real data. Be conversational, specific, and always show the numbers behind your answers.

## STRICT DATA ACCESS RULES

You may ONLY query these tables and columns:
- app_private.decision_log: created_at, fare, trip_minutes, trip_miles, pickup_minutes, mode_at_decision, decision_result->>'verdict' (values are ACCEPT or DECLINE), decision_result->>'reason', ping_h3_index, market_id, market_name, trace_data->>'source' (threshold cascade tier that fired: hex_cache, market_rate, or dignity_floor)
- app_private.hex_pricing_cache: h3_index, day_of_week, hour_of_day, revenue_hourly, revenue_mileage, ai_hourly, ai_mileage, picky_hourly, picky_mileage, sample_count, data_source, updated_at
- public.markets: id, name, metroplex_id

Personal data queries: always filter WHERE driver_id = '{DRIVER_ID}'
Market data queries: always aggregate - never return individual rows

## ABSOLUTE PROHIBITIONS
- NEVER generate INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE
- NEVER return raw driver_id values
- NEVER expose another driver's individual decisions or earnings
- NEVER access driver_settings_new or any auth/payment tables
- NEVER return more than 200 rows
- Maximum 3 queries per user message

## QUERY FORMAT
Wrap every SQL query in <query> tags like this:
<query>
SELECT ... FROM ... WHERE ... LIMIT ...
</query>

## DECISION ENGINE THRESHOLD LOGIC
Every offer is evaluated against a threshold: the highest of your neighborhood's recent earnings rate, the broader city rate, or a minimum floor — then adjusted up when demand is high nearby using a proprietary demand multiplier.

When explaining decisions to drivers, use plain language:
- "neighborhood rate" instead of hex cache rate
- "city rate" instead of market rate  
- "minimum floor" instead of dignity floor
- "demand multiplier" instead of hex pulse multiplier

## OPERATING MODES
The app has exactly three modes — use only these names:
- FREESTYLE: pure rate filtering, driver stays anywhere in the market
- PUDDLE_JUMP: stay within a high-value geographic zone, best for weekend nights
- TOWARDS: directional repositioning toward a target market

Never reference "AI mode", "PuddleJumper AI mode", or any other mode names. They do not exist.

## SHIFT BOUNDARY LOGIC
- Sun-Thu: 3am to midnight (America/Chicago)
- Fri: noon Fri to noon Sat (America/Chicago)
- Sat: noon Sat to noon Sun (America/Chicago)
- Database stores UTC - session timezone is pre-set, write plain local time strings

## TIME HANDLING
The database session timezone is already set to the driver's local timezone before your query runs. Write ALL datetime filters as plain local time strings in 'YYYY-MM-DD HH:MM:SS' format. Do NOT use AT TIME ZONE, TIMESTAMPTZ casts, or UTC offsets — PostgreSQL will handle the conversion automatically.

The current local time is {LOCAL_TIME}. A TEMPORAL ANCHORS block is prepended to the top of this prompt with exact pre-calculated dates. You MUST use those exact dates — do NOT calculate dates yourself. If the anchor says 'Last Saturday: 2026-03-14', use 2026-03-14. Never override the anchors with your own calculation.

When a driver uses relative time expressions like "this morning", "today", "yesterday", "last night", "this week" — ALWAYS translate them to a plain local time SQL filter. NEVER ask the driver to clarify a date or time. Make a reasonable assumption, state it briefly, and run the query. Assume weeks start on Sunday when calculating relative dates like "this week" or "last week".

Example — "how did I do this morning?" → respond: "Looking at your decisions from 6am to noon today:" then run:
<query>
SELECT decision_result->>'verdict' as verdict, fare, trip_miles, trip_minutes, decision_result->>'reason' as reason
FROM app_private.decision_log
WHERE driver_id = '{DRIVER_ID}'
AND created_at >= '{TODAY} 06:00:00'
AND created_at < '{TODAY} 12:00:00'
ORDER BY created_at
</query>

## ZERO RESULTS HANDLING
If a query returns 0 rows, do NOT immediately tell the driver they didn't drive. Instead:
1. Double-check your date filters using the TEMPORAL ANCHORS
2. Consider that the driver may have driven at different hours than assumed
3. Say "I didn't find data for that exact window — want me to check a broader range?"

## SHIFT vs CALENDAR DAY
When a driver asks about a specific day (e.g., "Saturday"), they mean their DRIVING SHIFT, not midnight to midnight. Use these shift boundaries:
- Friday shift: Friday noon to Saturday noon
- Saturday shift: Saturday noon to Sunday noon  
- Weekday shifts: 4am to midnight

## TONE
Talk like a knowledgeable driving partner. Lead with the answer, follow with the numbers. Keep it concise - drivers are busy."""

FORBIDDEN_KEYWORDS = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|CREATE|REPLACE)\b',
    re.IGNORECASE
)

MAX_QUERIES_PER_REQUEST = 3


def get_driver_profile(driver_id):
    """Fetch driver's key stats to inject as context."""
    from db import get_db_readonly, return_db_readonly
    conn = None
    try:
        conn = get_db_readonly()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) as total_decisions,
                    COUNT(CASE WHEN decision_result->>'verdict' = 'ACCEPT' THEN 1 END) as total_accepts,
                    ROUND(AVG(CASE WHEN decision_result->>'verdict' = 'ACCEPT' THEN fare END), 2) as avg_fare,
                    MODE() WITHIN GROUP (ORDER BY mode_at_decision) as primary_mode,
                    MAX(created_at) as last_seen
                FROM app_private.decision_log
                WHERE driver_id = %s
            """, (driver_id,))
            row = cur.fetchone()
            if row and row['total_decisions'] > 0:
                return (
                    f"DRIVER PROFILE: {row['total_accepts']} accepted trips out of "
                    f"{row['total_decisions']} total offers. "
                    f"Average accepted fare: ${row['avg_fare']}. "
                    f"Primary mode: {row['primary_mode']}. "
                    f"Last active: {row['last_seen'].strftime('%b %d, %Y') if row['last_seen'] else 'unknown'}."
                )
            return "DRIVER PROFILE: New driver, no trip history yet."
    except Exception as e:
        logger.error("DRIVER PROFILE ERROR: %s", str(e))
        return ""
    finally:
        if conn:
            return_db_readonly(conn)




VALID_TIMEZONE_RE = re.compile(r'^[A-Za-z_]+/[A-Za-z_]+$|^UTC$')

def get_driver_timezone(driver_id):
    """Fetch driver's timezone from settings. Validates format before use."""
    from db import get_db
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT settings->>'timezone' as timezone
                FROM app_private.driver_settings_new
                WHERE driver_id = %s
            """, (driver_id,))
            row = cur.fetchone()
            if row and row['timezone']:
                tz = row['timezone']
                if VALID_TIMEZONE_RE.match(tz):
                    return tz
                logger.warning("Invalid timezone format for driver %s: %s", driver_id, tz)
        return 'UTC'
    except Exception as e:
        logger.error("DRIVER TIMEZONE ERROR: %s", str(e))
        return 'UTC'
    finally:
        if conn:
            conn.close()

def load_history(driver_id):
    """Load last 10 exchanges from DB for this driver."""
    from db import get_db
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT role, content FROM app_private.intelligence_conversations
                WHERE driver_id = %s
                ORDER BY created_at DESC
                LIMIT 20
            """, (driver_id,))
            rows = cur.fetchall()
            # Reverse to get chronological order
            rows = list(reversed(rows))
            return [{"role": r["role"], "content": r["content"]} for r in rows]
    except Exception as e:
        logger.error("LOAD HISTORY ERROR: %s", str(e))
        return []
    finally:
        if conn:
            conn.close()


def save_exchange(driver_id, user_message, answer, queries_run=0):
    """Persist a user/assistant exchange to DB."""
    from db import get_db
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO app_private.intelligence_conversations
                    (driver_id, role, content, queries_run)
                VALUES (%s, 'user', %s, 0),
                       (%s, 'assistant', %s, %s)
            """, (driver_id, user_message, driver_id, answer, queries_run))
            conn.commit()
    except Exception as e:
        logger.error("SAVE EXCHANGE ERROR: %s", str(e))
    finally:
        if conn:
            conn.close()


def extract_queries(text):
    return re.findall(r'<query>(.*?)</query>', text, re.DOTALL)


def validate_query(sql):
    if FORBIDDEN_KEYWORDS.search(sql):
        raise ValueError("Forbidden SQL operation detected")
    return sql.strip()


def run_queries(queries, driver_id, timezone='UTC'):
    from db import get_db_readonly, return_db_readonly
    results = []
    conn = None
    try:
        conn = get_db_readonly()
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('timezone', %s, true);", (timezone,))
                for sql in queries[:MAX_QUERIES_PER_REQUEST]:
                    validated = validate_query(sql)
                    safe_sql = validated.replace('{DRIVER_ID}', driver_id)
                    for i in range(0, len(safe_sql), 200):
                        logger.info("SQL[%d]: %s", i, safe_sql[i:i+200])
                    cur.execute(safe_sql)
                    rows = cur.fetchall()
                    results.append([dict(r) for r in rows])
    finally:
        if conn:
            return_db_readonly(conn)
    return results


def call_claude(messages, driver_id, model="claude-sonnet-4-20250514", timezone='UTC'):
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    from datetime import datetime, timedelta
    import pytz
    profile = get_driver_profile(driver_id)
    try:
        tz = pytz.timezone(timezone)
        now = datetime.now(tz)
        local_now = now.strftime('%A, %B %d, %Y, %I:%M %p %Z')

        def get_last_day(target_day_index):
            days_back = (now.weekday() - target_day_index) % 7
            if days_back == 0:
                days_back = 7
            return (now - timedelta(days=days_back)).strftime('%Y-%m-%d')

        calendar_ref = (
            "\n--- TEMPORAL ANCHORS (use for all relative date calculations) ---\n"
            f"Today:               {now.strftime('%A, %Y-%m-%d')}\n"
            f"Yesterday:           {(now - timedelta(days=1)).strftime('%Y-%m-%d')}\n"
            f"Last Saturday:       {get_last_day(5)}\n"
            f"Last Friday:         {get_last_day(4)}\n"
            f"Last Sunday:         {get_last_day(6)}\n"
            f"Month-to-date start: {now.strftime('%Y-%m-01')}\n"
            f"Year-to-date start:  {now.strftime('%Y-01-01')}\n"
            "-----------------------------------------------------------------"
        )
    except Exception:
        local_now = datetime.utcnow().strftime('%A, %B %d, %Y, %I:%M %p UTC')
        calendar_ref = ""
    logger.info("TIMEZONE: %s LOCAL_TIME: %s", timezone, local_now)
    today_str = now.strftime('%Y-%m-%d') if 'now' in locals() else datetime.utcnow().strftime('%Y-%m-%d')
    system = SYSTEM_PROMPT.replace('{DRIVER_ID}', driver_id).replace('America/Chicago', timezone).replace('{LOCAL_TIME}', local_now).replace('{TODAY}', today_str)
    system = calendar_ref + system


    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=system,
        messages=messages
    )
    return response.content[0].text


@market_intelligence_bp.route('/v1/market-intelligence', methods=['POST'])
def market_intelligence():
    try:
        driver_id = verify_and_get_user_id(request)
        body = request.get_json()

        if not body or 'message' not in body:
            return jsonify({"error": "Missing message"}), 400

        user_message = body['message'].strip()
        if not user_message:
            return jsonify({"error": "Empty message"}), 400

        # Fetch timezone once for this request
        timezone = get_driver_timezone(driver_id)

        # Load persistent history from DB (last 10 exchanges)
        history = load_history(driver_id)
        messages = history + [{"role": "user", "content": user_message}]

        first_response = call_claude(messages, driver_id, model="claude-sonnet-4-20250514", timezone=timezone)
        queries = extract_queries(first_response)
        query_results = []

        if queries:
            try:
                query_results = run_queries(queries, driver_id, timezone)
            except ValueError as e:
                logger.warning("BLOCKED QUERY from driver %s: %s", driver_id, str(e))
                return jsonify({"error": "Query validation failed", "details": str(e)}), 400
            except Exception as e:
                logger.error("QUERY EXECUTION ERROR: %s", str(e))
                return jsonify({"error": "Query execution failed"}), 500

            result_message = {
                "role": "user",
                "content": f"Query results: {json.dumps(query_results, default=str)}\n\nNow answer the driver's question using these results."
            }
            messages_with_results = messages + [
                {"role": "assistant", "content": first_response},
                result_message
            ]
            final_response = call_claude(messages_with_results, driver_id, timezone=timezone)
        else:
            final_response = first_response

        clean_answer = re.sub(r'<query>.*?</query>', '', final_response, flags=re.DOTALL).strip()
        save_exchange(driver_id, user_message, clean_answer, len(queries))
        return jsonify({
            "answer": clean_answer,
            "queries_run": len(queries)
        })

    except Exception as e:
        logger.error("MARKET INTELLIGENCE ERROR: %s", str(e))
        return jsonify({"error": str(e)}), 500
