import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import redis

import app as proxy_app


def unused_local_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def valid_submission():
    return {
        "mode": "quick",
        "name": "Synthetic Test",
        "email": "synthetic@example.com",
        "emailCopyConsent": True,
        "website": "",
        "answers": {
            "employment": "employed-stable",
            "employmentNotes": "Synthetic note",
            "savings": 10000,
            "expenses": 2000,
            "runwayMonths": 5,
            "testing": "untested-idea",
            "testingNotes": "Synthetic note",
            "skills": "Synthetic skill",
            "confidence": "medium",
            "time": "4-7",
            "constraints": "Synthetic constraint",
        },
    }


class RedisRateLimitIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_path = os.environ.get("REDIS_SERVER_BIN") or shutil.which("redis-server")
        if not server_path:
            raise unittest.SkipTest("redis-server is required for real-Redis integration tests")

        cls.temp_dir = tempfile.TemporaryDirectory(prefix="moonshot-redis-test-")
        cls.port = unused_local_port()
        cls.redis_url = f"redis://127.0.0.1:{cls.port}/15"
        cls.server = subprocess.Popen(
            [
                server_path,
                "--bind", "127.0.0.1",
                "--port", str(cls.port),
                "--dir", cls.temp_dir.name,
                "--save", "",
                "--appendonly", "no",
                "--logfile", "",
                "--protected-mode", "yes",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.redis = redis.Redis.from_url(
            cls.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
        )
        ready_by = time.monotonic() + 5
        while time.monotonic() < ready_by:
            if cls.server.poll() is not None:
                cls.temp_dir.cleanup()
                raise RuntimeError("redis-server exited before becoming ready")
            try:
                cls.redis.ping()
                break
            except redis.RedisError:
                time.sleep(0.05)
        else:
            cls.server.terminate()
            cls.server.wait(timeout=5)
            cls.temp_dir.cleanup()
            raise RuntimeError("redis-server did not become ready within five seconds")

        cls.original_url = os.environ.get("RATE_LIMIT_REDIS_URL")
        cls.original_client = proxy_app._rate_limit_client
        cls.original_client_url = proxy_app._rate_limit_client_url
        os.environ["RATE_LIMIT_REDIS_URL"] = cls.redis_url
        proxy_app._rate_limit_client = cls.redis
        proxy_app._rate_limit_client_url = cls.redis_url

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "original_url", None) is None:
            os.environ.pop("RATE_LIMIT_REDIS_URL", None)
        else:
            os.environ["RATE_LIMIT_REDIS_URL"] = cls.original_url
        proxy_app._rate_limit_client = getattr(cls, "original_client", None)
        proxy_app._rate_limit_client_url = getattr(cls, "original_client_url", None)
        if getattr(cls, "server", None) and cls.server.poll() is None:
            cls.server.terminate()
            cls.server.wait(timeout=5)
        if getattr(cls, "temp_dir", None):
            cls.temp_dir.cleanup()

    def setUp(self):
        self.redis.flushdb()
        self.client = proxy_app.app.test_client()

    @staticmethod
    def upstream_response():
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"content": [{"type": "text", "text": "SYNTHETIC_OK"}]}
        return response

    def post_proxy(self, ip):
        payload = {
            "model": "claude-sonnet-4-5-20250929",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "Synthetic test only."}],
        }
        with patch.object(proxy_app.requests, "post", return_value=self.upstream_response()):
            return self.client.post(
                "/v1/messages",
                json=payload,
                environ_overrides={"REMOTE_ADDR": ip},
            )

    def test_real_redis_enforces_per_ip_429(self):
        with patch.dict(proxy_app.RATE_LIMIT_RULES["proxy"], {
            "per_ip_limit": 1,
            "per_ip_window": 60,
            "overall_limit": 100,
            "overall_window": 60,
        }):
            first = self.post_proxy("198.51.100.21")
            second = self.post_proxy("198.51.100.21")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertGreater(int(second.headers["Retry-After"]), 0)

    def test_real_redis_enforces_global_429_across_ips(self):
        with patch.dict(proxy_app.RATE_LIMIT_RULES["proxy"], {
            "per_ip_limit": 10,
            "per_ip_window": 60,
            "overall_limit": 1,
            "overall_window": 60,
        }):
            first = self.post_proxy("198.51.100.22")
            second = self.post_proxy("198.51.100.23")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertIn("busy", second.json["error"])

    def test_global_cap_does_not_allocate_new_ip_keys_after_limit(self):
        with patch.dict(proxy_app.RATE_LIMIT_RULES["proxy"], {
            "per_ip_limit": 10,
            "per_ip_window": 60,
            "overall_limit": 1,
            "overall_window": 60,
        }):
            self.assertEqual(self.post_proxy("198.51.100.24").status_code, 200)
            self.assertEqual(self.post_proxy("198.51.100.25").status_code, 429)
            self.assertEqual(self.post_proxy("198.51.100.26").status_code, 429)

        self.assertEqual(len(list(self.redis.scan_iter(match="moonshot:ratelimit:{proxy}:ip:*"))), 1)

    def test_real_redis_window_expiry_allows_request_again(self):
        with patch.dict(proxy_app.RATE_LIMIT_RULES["proxy"], {
            "per_ip_limit": 1,
            "per_ip_window": 1,
            "overall_limit": 100,
            "overall_window": 60,
        }):
            self.assertEqual(self.post_proxy("198.51.100.27").status_code, 200)
            limited = self.post_proxy("198.51.100.27")
            self.assertEqual(limited.status_code, 429)
            time.sleep(int(limited.headers["Retry-After"]) + 0.1)
            after_expiry = self.post_proxy("198.51.100.27")

        self.assertEqual(after_expiry.status_code, 200)

    def test_real_redis_outage_fails_closed(self):
        down_url = f"redis://127.0.0.1:{unused_local_port()}/0"
        with patch.dict(os.environ, {"RATE_LIMIT_REDIS_URL": down_url}):
            response = self.post_proxy("198.51.100.28")

        self.assertEqual(response.status_code, 503)
        self.assertIn("temporarily unavailable", response.json["error"])

    def test_submit_dry_run_uses_real_redis_but_never_sends_email(self):
        with patch.dict(os.environ, {"SMTP_DRY_RUN": "true"}, clear=False):
            with patch.object(proxy_app.smtplib, "SMTP") as starttls_smtp:
                with patch.object(proxy_app.smtplib, "SMTP_SSL") as ssl_smtp:
                    response = self.client.post(
                        "/submit",
                        json=valid_submission(),
                        headers={"Origin": "https://moonshotexitplanner.com"},
                        environ_overrides={"REMOTE_ADDR": "198.51.100.29"},
                    )

        self.assertEqual(response.status_code, 503)
        self.assertIn("no email was sent", response.json["error"])
        starttls_smtp.assert_not_called()
        ssl_smtp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
