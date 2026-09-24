import os
import json
import hashlib
import math
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from datetime import datetime, timezone
import requests
import redis
from redis.exceptions import RedisError
from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
try:
    TRUSTED_PROXY_HOPS = int(os.environ.get("TRUSTED_PROXY_HOPS", "0"))
except ValueError as error:
    raise RuntimeError("TRUSTED_PROXY_HOPS must be an integer") from error
if not 0 <= TRUSTED_PROXY_HOPS <= 5:
    raise RuntimeError("TRUSTED_PROXY_HOPS must be between 0 and 5")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=TRUSTED_PROXY_HOPS)
ALLOWED_ORIGINS = ["https://onebassplayer.github.io", "https://moonshotexitplanner.com", "https://www.moonshotexitplanner.com", "https://moonshotfitanalyzer.com", "https://www.moonshotfitanalyzer.com"]
ALLOWED_SUBMISSION_ORIGINS = {"https://onebassplayer.github.io", "https://moonshotexitplanner.com", "https://www.moonshotexitplanner.com"}
CORS(app, origins=ALLOWED_ORIGINS)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MAILCHIMP_API_KEY = os.environ.get("MAILCHIMP_API_KEY")
SUBMISSION_EMAIL_TO = "david@moonshotconsultingdc.com"
MAX_SUBMISSION_BYTES = 50_000
MAX_SUBMISSION_TEXT = 4_000

MC_DC           = "us9"
MC_AUDIENCE_ID  = "aae35b6cd9"
MC_API_BASE     = f"https://{MC_DC}.api.mailchimp.com/3.0"

MAX_BODY_BYTES = 200_000
app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
MAX_PROMPT_CHARS = 60_000
MAX_MESSAGES = 20
RATE_LIMIT_RULES = {
    "proxy": {
        "per_ip_limit": 10,
        "per_ip_window": 60,
        "overall_limit": 120,
        "overall_window": 60,
        "per_ip_message": "You're sending planner requests too quickly. Please wait a moment and try again.",
        "overall_message": "The planner is busy right now. Please try again shortly.",
    },
    "submit": {
        "per_ip_limit": 3,
        "per_ip_window": 600,
        "overall_limit": 30,
        "overall_window": 3600,
        "per_ip_message": "You've sent several submissions recently. Please wait before trying again.",
        "overall_message": "Submission email is temporarily busy. Please try again later.",
    },
}
RATE_LIMIT_SCRIPT = """
local per_ip_count = redis.call('INCR', KEYS[1])
if per_ip_count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
local overall_count = redis.call('INCR', KEYS[2])
if overall_count == 1 then redis.call('EXPIRE', KEYS[2], ARGV[2]) end
return {per_ip_count, overall_count}
"""
ALLOWED_MODELS = {
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-20250514",
    "claude-opus-4-20250514",
}
MAX_TOKENS_CEILING = 8000
_rate_limit_client = None
_rate_limit_client_url = None

def _bad(msg, code=400):
    return jsonify({"error": msg}), code

def _get_rate_limit_client():
    global _rate_limit_client, _rate_limit_client_url
    url = os.environ.get("RATE_LIMIT_REDIS_URL", "").strip()
    if not url:
        return None
    if url != _rate_limit_client_url:
        _rate_limit_client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        _rate_limit_client_url = url
    return _rate_limit_client

def _enforce_rate_limit(scope):
    rule = RATE_LIMIT_RULES[scope]
    try:
        client = _get_rate_limit_client()
    except (RedisError, ValueError):
        app.logger.warning("Shared rate-limit store could not be initialized")
        return _bad("Request protection is temporarily unavailable. Please try again shortly.", 503)
    if client is None:
        app.logger.warning("Shared rate-limit store is not configured")
        return _bad("Request protection is temporarily unavailable. Please try again shortly.", 503)

    now = time.time()
    per_ip_window = rule["per_ip_window"]
    overall_window = rule["overall_window"]
    per_ip_bucket = int(now // per_ip_window)
    overall_bucket = int(now // overall_window)
    per_ip_ttl = max(1, math.ceil((per_ip_bucket + 1) * per_ip_window - now))
    overall_ttl = max(1, math.ceil((overall_bucket + 1) * overall_window - now))
    remote_ip = request.remote_addr or "unknown"
    ip_fingerprint = hashlib.sha256(remote_ip.encode("utf-8")).hexdigest()
    per_ip_key = f"moonshot:ratelimit:{{{scope}}}:ip:{ip_fingerprint}:{per_ip_bucket}"
    overall_key = f"moonshot:ratelimit:{{{scope}}}:all:{overall_bucket}"

    try:
        per_ip_count, overall_count = client.eval(
            RATE_LIMIT_SCRIPT,
            2,
            per_ip_key,
            overall_key,
            per_ip_ttl,
            overall_ttl,
        )
    except (RedisError, OSError, ValueError, TypeError):
        app.logger.warning("Shared rate-limit store is unavailable")
        return _bad("Request protection is temporarily unavailable. Please try again shortly.", 503)

    if int(per_ip_count) > rule["per_ip_limit"]:
        response = jsonify({"error": rule["per_ip_message"]})
        response.status_code = 429
        response.headers["Retry-After"] = str(per_ip_ttl)
        return response
    if int(overall_count) > rule["overall_limit"]:
        response = jsonify({"error": rule["overall_message"]})
        response.status_code = 429
        response.headers["Retry-After"] = str(overall_ttl)
        return response
    return None

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
            return "Message role must be user or assistant"
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
            return "system must be a string"
        total_chars += len(system)
    if total_chars > MAX_PROMPT_CHARS:
        return f"Prompt too large ({total_chars} chars, max {MAX_PROMPT_CHARS})"
    return None

def _validate_submission(body):
    if not isinstance(body, dict):
        return "Request body must be a JSON object"
    mode = body.get("mode")
    if mode not in ("quick", "deep"):
        return "Submission mode must be quick or deep"
    allowed_keys = {"mode", "name", "email", "emailCopyConsent", "website", "answers" if mode == "quick" else "messages"}
    if set(body) - allowed_keys:
        return "Submission contains unsupported fields"
    if body.get("emailCopyConsent") is not True:
        return "Email-copy consent is required"
    if "website" in body and (not isinstance(body["website"], str) or len(body["website"]) > 500):
        return "Honeypot field is invalid"
    name = body.get("name")
    email = body.get("email")
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
        return "A valid name is required"
    if not isinstance(email, str) or len(email) > 254 or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return "A valid email address is required"
    if mode == "quick":
        answers = body.get("answers")
        required = {
            "employment", "employmentNotes", "savings", "expenses", "runwayMonths",
            "testing", "testingNotes", "skills", "confidence", "time", "constraints"
        }
        if not isinstance(answers, dict) or set(answers) != required:
            return "Quick Plan answers are incomplete or invalid"
        if answers["employment"] not in ("employed-stable", "employed-burned-out", "laid-off", "freelancing"):
            return "Employment selection is invalid"
        if answers["testing"] not in ("paid-or-committed", "strong-interest", "untested-idea", "no-specific-idea"):
            return "Business testing selection is invalid"
        if answers["confidence"] not in ("high", "medium", "low"):
            return "Confidence selection is invalid"
        if answers["time"] not in ("0-3", "4-7", "8-15", "16+"):
            return "Weekly time selection is invalid"
        for key in ("employmentNotes", "testingNotes", "skills", "constraints"):
            value = answers[key]
            if not isinstance(value, str) or len(value) > MAX_SUBMISSION_TEXT:
                return f"{key} must be text of at most {MAX_SUBMISSION_TEXT} characters"
        for key in ("savings", "expenses", "runwayMonths"):
            value = answers[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or value > 1_000_000_000:
                return f"{key} must be a non-negative number"
    else:
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages or len(messages) > 20:
            return "Deep Dive must contain between 1 and 20 messages"
        if not any(isinstance(message, dict) and message.get("role") == "user" for message in messages):
            return "Deep Dive must contain at least one user message"
        for message in messages:
            if not isinstance(message, dict) or set(message) != {"role", "text"} or message.get("role") not in ("user", "assistant"):
                return "Each Deep Dive message must have a valid role"
            content = message.get("text")
            if not isinstance(content, str) or not content.strip() or len(content) > MAX_SUBMISSION_TEXT:
                return f"Each Deep Dive message must contain 1 to {MAX_SUBMISSION_TEXT} characters"
    return None

def _submission_mail_settings():
    host = os.environ.get("SMTP_HOST", "").strip()
    sender = os.environ.get("SMTP_FROM_EMAIL", "").strip()
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    security = os.environ.get("SMTP_SECURITY", "starttls").strip().lower()
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
    except ValueError:
        return None
    if not host or not sender or not 1 <= port <= 65535:
        return None
    if security not in ("starttls", "ssl") or bool(username) != bool(password):
        return None
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", sender):
        return None
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", SUBMISSION_EMAIL_TO):
        return None
    return host, port, sender, username, password, security

def _send_submission_email(body):
    dry_run = os.environ.get("SMTP_DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")
    if dry_run:
        return "SMTP dry-run is enabled; no email was sent", 503
    settings = _submission_mail_settings()
    if not settings:
        return "Submission email is not configured", 503
    host, port, sender, username, password, security = settings
    mode = body["mode"]
    submitted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    message = EmailMessage()
    message["Subject"] = f"Moonshot Exit Planner submission ({mode.title()})"
    message["From"] = sender
    message["To"] = SUBMISSION_EMAIL_TO
    message["Reply-To"] = body["email"]
    details = {"answers": body["answers"]} if mode == "quick" else {"conversation": body["messages"]}
    message.set_content(
        "New Moonshot Exit Planner submission\n\n"
        f"Path: {mode.title()}\n"
        f"Name: {body['name'].strip()}\n"
        f"Email: {body['email'].strip()}\n"
        "Email-copy consent: Confirmed in the planner\n"
        f"Received (UTC): {submitted_at}\n\n"
        "Full submission:\n"
        f"{json.dumps(details, ensure_ascii=False, indent=2)}\n"
    )
    try:
        if security == "ssl":
            with smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context()) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
    except (OSError, smtplib.SMTPException, ValueError) as error:
        app.logger.warning("Submission email delivery failed (%s)", type(error).__name__)
        return "Submission email could not be delivered", 502
    return None

@app.route("/v1/messages", methods=["POST"])
@app.route("/proxy", methods=["POST"])
def proxy():
    limited = _enforce_rate_limit("proxy")
    if limited:
        return limited
    raw = request.get_data(cache=False)
    if not raw:
        return _bad("Empty request body")
    if len(raw) > MAX_BODY_BYTES:
        return _bad(f"Request body too large (max {MAX_BODY_BYTES} bytes)", 413)
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return _bad("Invalid JSON")
    err = _validate_payload(body)
    if err:
        return _bad(err)
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
    try:
        body = request.get_json(force=True, silent=True) or {}
    except Exception:
        return _bad("Invalid JSON")
    email = (body.get("email") or "").strip().lower()
    first_name = (body.get("firstName") or "").strip()[:100]
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
    if resp.status_code == 200:
        return jsonify({"subscribed": True}), 200
    mc_body = resp.json()
    if resp.status_code == 400 and mc_body.get("title") == "Member Exists":
        return jsonify({"subscribed": True, "note": "already_subscribed"}), 200
    return jsonify({"subscribed": False, "detail": mc_body.get("detail", "unknown")}), 200

@app.route("/submit", methods=["POST"])
def submit():
    origin = request.headers.get("Origin")
    if origin not in ALLOWED_SUBMISSION_ORIGINS:
        return _bad("Submission origin is not allowed", 403)
    limited = _enforce_rate_limit("submit")
    if limited:
        return limited
    raw = request.get_data(cache=False)
    if not raw:
        return _bad("Empty request body")
    if len(raw) > MAX_SUBMISSION_BYTES:
        return _bad(f"Submission too large (max {MAX_SUBMISSION_BYTES} bytes)", 413)
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return _bad("Invalid JSON")
    if isinstance(body, dict) and isinstance(body.get("website"), str) and body["website"].strip():
        return jsonify({"submitted": True}), 200
    err = _validate_submission(body)
    if err:
        return _bad(err)
    err = _send_submission_email(body)
    if err:
        return _bad(*err)
    return jsonify({"submitted": True}), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
