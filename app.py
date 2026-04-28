import os
import json
import re
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins=["https://onebassplayer.github.io", "https://moonshotexitplanner.com", "https://moonshotfitanalyzer.com"])
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MAILCHIMP_API_KEY = os.environ.get("MAILCHIMP_API_KEY")

# --- Mailchimp config ---
MC_DC           = "us9"
MC_AUDIENCE_ID  = "aae35b6cd9"
MC_API_BASE     = f"https://{MC_DC}.api.mailchimp.com/3.0"

# --- Server-side input validation limits ---
MAX_BODY_BYTES = 200_000          # 200 KB hard cap on raw request body
MAX_PROMPT_CHARS = 60_000         # combined user/system text length
MAX_MESSAGES = 20                 # cap conversation length
ALLOWED_MODELS = {
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-20250514",
    "claude-opus-4-20250514",
}
MAX_TOKENS_CEILING = 8000

def _bad(msg, code=400):
    return jsonify({"error": msg}), code

def _validate_payload(body):
    if not isinstance(body, dict):
        return "Request body must be a JSON object"
    model = body.get("model")
    if not isinstance(model, str) or model not in ALLOWED_MODELS:
        return "Invalid or disallowed model"
    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, int) or max_tokens < 1:
        return "'max_tokens' must be a positive integer"
    if max_tokens > MAX_TOKENS_CEILING:
        return f"'max_tokens' exceeds ceiling of {MAX_TOKENS_CEILING}"
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) == 0:
        return "'messages' must be a non-empty array"
    if len(messages) > MAX_MESSAGES:
        return f"Too many messages (max {MAX_MESSAGES})"
    total_chars = 0
    for m in messages:
        if not isinstance(m, dict):
            return "Each message must be an object"
        role = m.get("role")
        if role not in ("user", "assistant"):
            return "Message role must be 'user' or 'assistant'"
        content = m.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total_chars += len(part["text"])
                else:
                    return "Unsupported message content part"
        else:
            return "Message content must be string or array"
    system = body.get("system")
    if system is not None:
        if not isinstance(system, str):
            return "'system' must be a string"
        total_chars += len(system)
    if total_chars > MAX_PROMPT_CHARS:
        return f"Prompt too large ({total_chars} chars, max {MAX_PROMPT_CHARS})"
    return None

@app.route("/proxy", methods=["POST"])
def proxy():
    # 1. Raw body size check
    raw = request.get_data(cache=False)
    if not raw:
        return _bad("Empty request body")
    if len(raw) > MAX_BODY_BYTES:
        return _bad(f"Request body too large (max {MAX_BODY_BYTES} bytes)", 413)

    # 2. Parse JSON from the already-read bytes (avoids double-read of stream)
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return _bad("Invalid JSON")

    # 3. Schema / size validation
    err = _validate_payload(body)
    if err:
        return _bad(err)

    # 4. Forward to Anthropic
    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            },
            json=body,
            timeout=120
        )
    except requests.RequestException as e:
        return _bad(f"Upstream error: {type(e).__name__}", 502)

    try:
        return jsonify(response.json()), response.status_code
    except ValueError:
        return _bad("Upstream returned non-JSON", 502)

@app.route("/subscribe", methods=["POST"])
def subscribe():
    """Silently subscribe a user to the Moonshot Consulting Mailchimp audience."""
    try:
        body = request.get_json(force=True, silent=True) or {}
    except Exception:
        return _bad("Invalid JSON")

    email = (body.get("email") or "").strip().lower()
    first_name = (body.get("firstName") or "").strip()[:100]

    # Basic email validation
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return _bad("Invalid email address")

    if not MAILCHIMP_API_KEY:
        return _bad("Mailchimp not configured", 503)

    mc_url = f"{MC_API_BASE}/lists/{MC_AUDIENCE_ID}/members"
    payload = {
        "email_address": email,
        "status": "subscribed",
        "merge_fields": {"FNAME": first_name},
        "tags": ["moonshot-exit-planner"]
    }

    try:
        resp = requests.post(
            mc_url,
            auth=("anystring", MAILCHIMP_API_KEY),
            json=payload,
            timeout=10
        )
    except requests.RequestException as e:
        return _bad(f"Mailchimp unreachable: {type(e).__name__}", 502)

    # 200 = new subscriber, 400 with "Member Exists" = already on list (treat as ok)
    if resp.status_code == 200:
        return jsonify({"subscribed": True}), 200
    mc_body = resp.json()
    if resp.status_code == 400 and mc_body.get("title") == "Member Exists":
        return jsonify({"subscribed": True, "note": "already_subscribed"}), 200
    # Any other error — log but don't break user flow
    return jsonify({"subscribed": False, "detail": mc_body.get("detail", "unknown")}), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
import os
import json
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins=["https://onebassplayer.github.io", "https://moonshotexitplanner.com", "https://moonshotfitanalyzer.com"])
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# --- Server-side input validation limits ---
MAX_BODY_BYTES = 200_000          # 200 KB hard cap on raw request body
MAX_PROMPT_CHARS = 60_000         # combined user/system text length
MAX_MESSAGES = 20                 # cap conversation length
ALLOWED_MODELS = {
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-20250514",
    "claude-opus-4-20250514",
}
MAX_TOKENS_CEILING = 8000

def _bad(msg, code=400):
    return jsonify({"error": msg}), code

def _validate_payload(body):
    if not isinstance(body, dict):
        return "Request body must be a JSON object"
    model = body.get("model")
    if not isinstance(model, str) or model not in ALLOWED_MODELS:
        return "Invalid or missing 'model'"
    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, int) or max_tokens <= 0 or max_tokens > MAX_TOKENS_CEILING:
        return f"'max_tokens' must be 1..{MAX_TOKENS_CEILING}"
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) == 0:
        return "'messages' must be a non-empty array"
    if len(messages) > MAX_MESSAGES:
        return f"Too many messages (max {MAX_MESSAGES})"
    total_chars = 0
    for m in messages:
        if not isinstance(m, dict):
            return "Each message must be an object"
        role = m.get("role")
        if role not in ("user", "assistant"):
            return "Message role must be 'user' or 'assistant'"
        content = m.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total_chars += len(part["text"])
                else:
                    return "Unsupported message content part"
        else:
            return "Message content must be string or array"
    system = body.get("system")
    if system is not None:
        if not isinstance(system, str):
            return "'system' must be a string"
        total_chars += len(system)
    if total_chars > MAX_PROMPT_CHARS:
        return f"Prompt too large ({total_chars} chars, max {MAX_PROMPT_CHARS})"
    return None

@app.route("/v1/messages", methods=["POST"])
def proxy():
    if not ANTHROPIC_API_KEY:
        return _bad("API key not configured", 500)

    # 1. Hard cap on raw body size before parsing
    raw = request.get_data(cache=True)
    if not raw:
        return _bad("Empty request body")
    if len(raw) > MAX_BODY_BYTES:
        return _bad(f"Request body too large (max {MAX_BODY_BYTES} bytes)", 413)

    # 2. Parse JSON from the already-read bytes (avoids double-read of stream)
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return _bad("Invalid JSON")

    # 3. Schema / size validation
    err = _validate_payload(body)
    if err:
        return _bad(err)

    # 4. Forward to Anthropic
    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            },
            json=body,
            timeout=120
        )
    except requests.RequestException as e:
        return _bad(f"Upstream error: {type(e).__name__}", 502)

    try:
        return jsonify(response.json()), response.status_code
    except ValueError:
        return _bad("Upstream returned non-JSON", 502)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
