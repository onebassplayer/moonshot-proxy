# Moonshot Proxy

## Exit Planner configuration

The planner endpoints fail closed if the shared rate-limit store is not configured. Set service values only in the private Render environment; never put credentials in the website, source control, logs, or request bodies.

- `RATE_LIMIT_REDIS_URL`: private connection URL for a shared Redis-compatible store. Provision the store before deploying this branch; local or per-process counters are deliberately not used.
- `TRUSTED_PROXY_HOPS`: number of trusted forwarding proxies. Defaults to `0`. Set this to the verified Render proxy-hop count before relying on per-IP limits. Do not trust `X-Forwarded-For` directly or guess the hop count.
- `ANTHROPIC_API_KEY`: the Anthropic API key used by `/v1/messages` and `/proxy`. David privately replaced this key in Render on 2026-09-24, and a later synthetic request using the allow-listed model succeeded with HTTP 200. The earlier PR-body 401 note is superseded and is not a reason to rotate the key again. No key value is stored in this repository.

## Rate limits

Redis applies atomic fixed-window limits across all workers. The overall counter is checked before a per-IP key is created, so over-limit global traffic cannot create unbounded new IP keys. Rate-limit keys contain a SHA-256 fingerprint of the resolved client IP, not the raw IP, and expire with the corresponding window.

| Route family | Per IP | Overall |
| --- | --- | --- |
| `/v1/messages` and `/proxy` | 10 requests / 60 seconds | 120 requests / 60 seconds |
| `/submit` | 3 requests / 10 minutes | 30 requests / hour |

Excess requests receive HTTP `429`, a friendly message, and `Retry-After`. If Redis is missing or unavailable, these endpoints return HTTP `503` instead of processing an unprotected request. The one-proxy-hop setting must be verified against the Render service before configuring `TRUSTED_PROXY_HOPS`.

## Submission email

`POST /submit` requires `emailCopyConsent: true`, validates the origin and payload, and sends the full Quick Plan or Deep Dive submission to the fixed recipient `david@moonshotconsultingdc.com`. The route does not call Mailchimp or store the submission in an application database. If delivery is missing or fails, the planner offers retry or continuing without the email copy.

Configure the relay only in Render's private environment settings:

- `SMTP_HOST`: relay hostname.
- `SMTP_PORT`: `587` for STARTTLS or `465` for implicit TLS.
- `SMTP_SECURITY`: `starttls` (default) or `ssl`.
- `SMTP_FROM_EMAIL`: sender address accepted by the relay.
- `SMTP_USERNAME` and `SMTP_PASSWORD`: optional, but set both or neither.
- `SMTP_DRY_RUN`: set to `true` only in local/staging tests. The route then returns an explicit error and sends no message; it must not be enabled for production submissions.

No provider has been selected and no production SMTP credentials are configured by this branch. Test delivery with synthetic data in staging after David selects a provider and adds credentials privately.

## Logging and retention

The application does not intentionally log prompt, assessment, transcript, submission-email, or raw upstream response bodies. SMTP failures log only the exception class. Render request/access-log retention and Anthropic account processing, logging, and retention are controlled outside this repository and remain unverified. The SMTP provider and recipient mailbox may also retain or expose message copies according to their own settings; review those settings and deletion handling before publication.

## Tests

Run focused tests with:

```sh
python -m unittest discover -s tests -v
```

The unit suite uses synthetic values, a fake Redis counter, and mocked SMTP/upstream clients. When `redis-server` is available on `PATH` (or `REDIS_SERVER_BIN` names its executable), the integration suite starts an isolated, loopback-only Redis instance and tests real `429` limits, window expiry, Redis-outage fail-closed behavior, and SMTP dry-run with no send. If the server executable is unavailable, that integration test class is skipped. Tests do not require production credentials and never send real email. A real Anthropic authentication/response test and a real SMTP delivery test remain separate checks.
