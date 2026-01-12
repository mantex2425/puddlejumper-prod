from flask import Blueprint, request, jsonify
import redis
import json
import hashlib
import os
from anthropic import Anthropic
from utils import (
    verify_and_get_user_id,
    require_firebase_auth,
)

chat_ai_bp = Blueprint('chat_ai', __name__)

# Initialize Redis connection
r = redis.Redis(host='localhost', port=6379, decode_responses=True)

# Initialize Anthropic client
client = Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

# System prompt for Puddles
PUDDLES_SYSTEM_PROMPT = """You are Puddles, a cheeky frog co-pilot for rideshare drivers using the PuddleJumper app in Houston, Texas.

CRITICAL: Keep responses to ONE sentence maximum. Be concise and actionable.
Never use emojis or markdown formatting - just plain speech.

YOUR THREE ROLES (Handle All Seamlessly):
1. APP SUPPORT EXPERT - Answer questions about PuddleJumper features
2. RIDESHARE STRATEGIST - Provide Houston market intelligence and earnings advice
3. COMPANION - Chat about sports (Astros, Texans), weather, or life to break up tedious shifts

PUDDLEJUMPER KEY CONCEPTS:
- RED ZONES: Poor earnings (long suburb runs, low fares, traffic). Recommend REJECT.
- GREEN ZONES: High-value (airports IAH/Hobby, Medical Center, Galleria, events). Recommend ACCEPT.
- GRAY ZONES: Context-dependent based on time/location.

HOUSTON MARKETS:
- Airports: IAH (north), Hobby (southeast) - consistent demand
- High-value: Medical Center (24/7), Galleria (shopping), Downtown (business/events)
- Events: Minute Maid (Astros), NRG Stadium (Texans/rodeo), Toyota Center (Rockets)

YOUR PERSONALITY:
- Witty but helpful - occasional frog puns, don't overdo it
- Concise - drivers are working, keep responses 2-4 sentences
- Houston-savvy - know neighborhoods, teams, traffic patterns
- Empathetic - acknowledge driving can be tedious/stressful

Current driver context: {status} near {location} at {time}

Respond naturally. Blend support, strategy, and companionship seamlessly."""

@require_firebase_auth 
@chat_ai_bp.route('/chat-ai', methods=['POST'])
def chat():
    """
    sends json to anthropic bot
    """
    try:
       driver_id = verify_and_get_user_id(request)
    except Exception as e:
        print(f"Authentication failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 401

    try:
        data = request.json
        new_msg = data.get('message')
        context_hash = data.get('context_hash')
        session_id = data.get('session_id')
        context_data = data.get('context', {})
        
        if not new_msg:
            return jsonify({"error": "Message required"}), 400
        
        # Build dynamic system prompt with context
        system_prompt = PUDDLES_SYSTEM_PROMPT.format(
            status=context_data.get('status', 'driving'),
            location=context_data.get('location', 'Houston'),
            time=context_data.get('time', 'now')
        )
        
        # Retrieve conversation history from cache
        if context_hash:
            history_str = r.get(f"chat:{context_hash}")
            history = json.loads(history_str) if history_str else []
        else:
            history = []
        
        # Append new user message
        history.append({"role": "user", "content": new_msg})
        
        # Call Anthropic API
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"}
            }],
            messages=history,
            max_tokens=200
        )
        
        # Extract reply
        reply = response.content[0].text
        
        # Update history with assistant response
        history.append({"role": "assistant", "content": reply})
        
        # Create new hash for updated history
        history_str = json.dumps(history)
        new_hash = hashlib.sha256(history_str.encode()).hexdigest()
        
        # Store in Redis with 2-hour expiration
        r.set(f"chat:{new_hash}", history_str, ex=7200)
        
        return jsonify({
            "reply": reply,
            "new_context_hash": new_hash
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500
