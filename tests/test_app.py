import smtplib
import unittest
from unittest.mock import MagicMock, patch

import app as proxy_app


class MemoryRedis:
    def __init__(self):
        self.counts = {}

    def eval(self, script, key_count, *arguments):
        self.asserted_script = script
        self.asserted_key_count = key_count
        per_ip_key, overall_key = arguments[:2]
        self.counts[per_ip_key] = self.counts.get(per_ip_key, 0) + 1
        self.counts[overall_key] = self.counts.get(overall_key, 0) + 1
        return self.counts[per_ip_key], self.counts[overall_key]


def valid_quick_submission(consent=True):
    return {
        "mode": "quick",
        "name": "Synthetic Test",
        "email": "synthetic@example.com",
        "emailCopyConsent": consent,
        "website": "",
        "answers": {
            "employment": "employed-stable",
            "employmentNotes": "Synthetic note",
            "savings": 10000,
            "expenses": 2000,
            "runwayMonths": 5,
            "testing": "untested-idea",
            "testingNotes": "Synthetic note",
            "skills": "Synthetic test skill",
            "confidence": "medium",
            "time": "4-7",
            "constraints": "Synthetic constraint",
        },
    }


def valid_deep_submission():
    return {
        "mode": "deep",
        "name": "Not provided",
        "email": "synthetic@example.com",
        "emailCopyConsent": True,
        "website": "",
        "messages": [
            {"role": "assistant", "text": "Synthetic opening question"},
            {"role": "user", "text": "Synthetic transcript detail"},
        ],
    }


class MoonshotProxyTests(unittest.TestCase):
    def setUp(self):
        self.redis = MemoryRedis()
        self.redis_patch = patch.object(proxy_app, "_get_rate_limit_client", return_value=self.redis)
        self.redis_patch.start()
        self.client = proxy_app.app.test_client()

    def tearDown(self):
        self.redis_patch.stop()

    @staticmethod
    def proxy_payload(model="claude-sonnet-4-5-20250929"):
        return {
            "model": model,
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "Reply exactly SYNTHETIC_OK."}],
        }

    def mock_upstream(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"content": [{"type": "text", "text": "SYNTHETIC_OK"}]}
        return patch.object(proxy_app.requests, "post", return_value=response)

    def test_rejected_model_fails_before_upstream(self):
        with patch.object(proxy_app.requests, "post") as upstream:
            response = self.client.post("/v1/messages", json=self.proxy_payload("claude-opus-4-5"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"], "Invalid or disallowed model")
        upstream.assert_not_called()

    def test_allowed_model_reaches_upstream(self):
        with self.mock_upstream() as upstream:
            response = self.client.post("/v1/messages", json=self.proxy_payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["content"][0]["text"], "SYNTHETIC_OK")
        self.assertEqual(upstream.call_args.kwargs["json"]["model"], "claude-sonnet-4-5-20250929")

    def test_proxy_per_ip_limit_returns_friendly_429(self):
        with patch.dict(proxy_app.RATE_LIMIT_RULES["proxy"], {"per_ip_limit": 1}):
            with self.mock_upstream():
                first = self.client.post(
                    "/proxy", json=self.proxy_payload(), environ_overrides={"REMOTE_ADDR": "198.51.100.9"}
                )
                second = self.client.post(
                    "/proxy", json=self.proxy_payload(), environ_overrides={"REMOTE_ADDR": "198.51.100.9"}
                )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertIn("wait", second.json["error"])
        self.assertGreater(int(second.headers["Retry-After"]), 0)

    def test_proxy_overall_limit_applies_across_ips(self):
        with patch.dict(proxy_app.RATE_LIMIT_RULES["proxy"], {"overall_limit": 1}):
            with self.mock_upstream():
                first = self.client.post(
                    "/v1/messages", json=self.proxy_payload(), environ_overrides={"REMOTE_ADDR": "198.51.100.9"}
                )
                second = self.client.post(
                    "/v1/messages", json=self.proxy_payload(), environ_overrides={"REMOTE_ADDR": "198.51.100.10"}
                )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertIn("busy", second.json["error"])

    def test_rate_limit_fails_closed_without_shared_store(self):
        with patch.object(proxy_app, "_get_rate_limit_client", return_value=None):
            response = self.client.post("/v1/messages", json=self.proxy_payload())
        self.assertEqual(response.status_code, 503)
        self.assertIn("temporarily unavailable", response.json["error"])

    def test_submission_requires_explicit_consent(self):
        with patch.object(proxy_app, "_send_submission_email") as sender:
            response = self.client.post(
                "/submit",
                json=valid_quick_submission(consent=False),
                headers={"Origin": "https://moonshotexitplanner.com"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("consent", response.json["error"])
        sender.assert_not_called()

    def test_submission_per_ip_limit_returns_friendly_429(self):
        with patch.dict(proxy_app.RATE_LIMIT_RULES["submit"], {"per_ip_limit": 1}):
            with patch.dict("os.environ", {"SMTP_DRY_RUN": "true"}, clear=False):
                first = self.client.post(
                    "/submit",
                    json=valid_quick_submission(),
                    headers={"Origin": "https://moonshotexitplanner.com"},
                    environ_overrides={"REMOTE_ADDR": "198.51.100.11"},
                )
                second = self.client.post(
                    "/submit",
                    json=valid_quick_submission(),
                    headers={"Origin": "https://moonshotexitplanner.com"},
                    environ_overrides={"REMOTE_ADDR": "198.51.100.11"},
                )
        self.assertEqual(first.status_code, 503)
        self.assertEqual(second.status_code, 429)
        self.assertIn("wait", second.json["error"])
        self.assertGreater(int(second.headers["Retry-After"]), 0)

    def test_submission_overall_limit_applies_across_ips(self):
        with patch.dict(proxy_app.RATE_LIMIT_RULES["submit"], {"overall_limit": 1}):
            with patch.dict("os.environ", {"SMTP_DRY_RUN": "true"}, clear=False):
                first = self.client.post(
                    "/submit",
                    json=valid_quick_submission(),
                    headers={"Origin": "https://moonshotexitplanner.com"},
                    environ_overrides={"REMOTE_ADDR": "198.51.100.11"},
                )
                second = self.client.post(
                    "/submit",
                    json=valid_quick_submission(),
                    headers={"Origin": "https://moonshotexitplanner.com"},
                    environ_overrides={"REMOTE_ADDR": "198.51.100.12"},
                )
        self.assertEqual(first.status_code, 503)
        self.assertEqual(second.status_code, 429)
        self.assertIn("busy", second.json["error"])

    def test_smtp_dry_run_never_sends_mail(self):
        with patch.dict("os.environ", {"SMTP_DRY_RUN": "true"}, clear=False):
            with patch.object(proxy_app.smtplib, "SMTP") as smtp:
                result = proxy_app._send_submission_email(valid_quick_submission())
        self.assertEqual(result, ("SMTP dry-run is enabled; no email was sent", 503))
        smtp.assert_not_called()

    def test_smtp_starttls_sends_full_synthetic_submission(self):
        smtp = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = smtp
        env = {
            "SMTP_HOST": "smtp.example.test",
            "SMTP_PORT": "587",
            "SMTP_SECURITY": "starttls",
            "SMTP_FROM_EMAIL": "planner@example.test",
            "SMTP_USERNAME": "synthetic-user",
            "SMTP_PASSWORD": "synthetic-password",
            "SMTP_DRY_RUN": "false",
        }
        with patch.dict("os.environ", env, clear=False):
            with patch.object(proxy_app.smtplib, "SMTP", return_value=context):
                result = proxy_app._send_submission_email(valid_quick_submission())
        self.assertIsNone(result)
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("synthetic-user", "synthetic-password")
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "david@moonshotconsultingdc.com")
        self.assertEqual(message["Reply-To"], "synthetic@example.com")
        self.assertIn("Synthetic test skill", message.get_content())
        self.assertIn("Email-copy consent: Confirmed", message.get_content())

    def test_smtp_ssl_sends_full_deep_dive_transcript(self):
        smtp = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = smtp
        env = {
            "SMTP_HOST": "smtp.example.test",
            "SMTP_PORT": "465",
            "SMTP_SECURITY": "ssl",
            "SMTP_FROM_EMAIL": "planner@example.test",
            "SMTP_DRY_RUN": "false",
        }
        with patch.dict("os.environ", env, clear=False):
            with patch.object(proxy_app.smtplib, "SMTP_SSL", return_value=context):
                result = proxy_app._send_submission_email(valid_deep_submission())
        self.assertIsNone(result)
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "david@moonshotconsultingdc.com")
        self.assertEqual(message["Reply-To"], "synthetic@example.com")
        self.assertIn("Synthetic transcript detail", message.get_content())

    def test_smtp_failure_log_does_not_include_submission_content(self):
        smtp = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = smtp
        smtp.starttls.side_effect = smtplib.SMTPException("synthetic failure")
        env = {
            "SMTP_HOST": "smtp.example.test",
            "SMTP_PORT": "587",
            "SMTP_FROM_EMAIL": "planner@example.test",
            "SMTP_DRY_RUN": "false",
        }
        with patch.dict("os.environ", env, clear=False):
            with patch.object(proxy_app.smtplib, "SMTP", return_value=context):
                with self.assertLogs(proxy_app.app.logger, level="WARNING") as captured:
                    result = proxy_app._send_submission_email(valid_quick_submission())
        self.assertEqual(result, ("Submission email could not be delivered", 502))
        output = " ".join(captured.output)
        self.assertNotIn("Synthetic test skill", output)
        self.assertNotIn("synthetic@example.com", output)


if __name__ == "__main__":
    unittest.main()
