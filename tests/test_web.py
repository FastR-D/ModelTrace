"""Validate the public deployment boundary and Flask error handling offline."""

import hashlib
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from app import PROJECT, app
from modeltrace import APIError


class WebBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.config = patch.dict(app.config, {"PUBLIC_MODE": True, "READ_ONLY": True})
        self.config.start()
        self.addCleanup(self.config.stop)
        environment = patch.dict(os.environ, {"MODELTRACE_ALLOWED_HOSTS": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.payload = {"base_url": "https://api.example.com/v1", "api_key": "test-secret-key",
                        "api_model": "mock", "prompt": "Generate numbers", "expected_count": 300,
                        "api_format": "responses"}

    def test_cloud_ui_and_write_routes_are_read_only(self):
        bank = PROJECT / "data" / "unified_bank.json"
        before = hashlib.sha256(bank.read_bytes()).hexdigest()
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="test-api" class="mode-panel active"', page)
        self.assertNotIn('id="auto-enrollment"', page)
        for route in ("/api/banks", "/api/enroll/auto"):
            response = self.client.post(route, json={})
            self.assertEqual(response.status_code, 403)
        self.assertEqual(before, hashlib.sha256(bank.read_bytes()).hexdigest())

    def test_cloud_uses_bounded_single_probe_and_no_cache(self):
        result = {"text": "1, 2", "accepted": False, "parsed_numbers": 2,
                  "minimum_numbers": 165, "api_format": "responses"}
        with patch("app.probe_api", return_value=result) as probe:
            response = self.client.post("/api/test/probe", json=self.payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertTrue(probe.call_args.kwargs["public_only"])
        self.assertEqual(probe.call_args.kwargs["timeout"], 60)
        self.assertEqual(probe.call_args.kwargs["retries"], 0)
        self.assertEqual(probe.call_args.kwargs["api_format"], "responses")
        self.assertEqual(self.client.post("/api/test/auto", json=self.payload).status_code, 409)

    def test_invalid_json_fields_and_large_bodies_return_json(self):
        cases = [None, [], {}, {**self.payload, "expected_count": True},
                 {**self.payload, "temperature": "nan"}, {**self.payload, "api_format": "unknown"}]
        for payload in cases:
            with self.subTest(payload=payload):
                response = self.client.post("/api/test/probe", json=payload,
                                            content_type="application/json")
                self.assertEqual(response.status_code, 400)
                self.assertIsInstance(response.json["error"], str)
        response = self.client.post("/api/test/probe", data="{broken", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/test/probe", data="x" * (256 * 1024 + 1),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 413)

    def test_upstream_error_redacts_key_and_preserves_status(self):
        error = APIError("HTTP 401: " + self.payload["api_key"], status_code=401)
        with patch("app.probe_api", side_effect=error):
            response = self.client.post("/api/test/probe", json=self.payload)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json["upstream_status"], 401)
        self.assertNotIn(self.payload["api_key"], response.get_data(as_text=True))

    def test_private_http_and_foreign_origin_are_rejected(self):
        payload = {**self.payload, "base_url": "http://127.0.0.1:7860"}
        response = self.client.post("/api/test/probe", json=payload)
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/test/probe", json=self.payload,
                                    headers={"Origin": "https://other.example"})
        self.assertEqual(response.status_code, 403)

    def test_gateway_failures_have_readable_messages_and_redacted_diagnostics(self):
        detail = "HTTP 502: Origin incomplete " + self.payload["api_key"] + " (Ray ID: a47bb9434cf6e670)"
        with patch("app.probe_api", side_effect=APIError(detail, status_code=502, retryable=True)):
            response = self.client.post("/api/test/probe", json=self.payload)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json["upstream_status"], 502)
        self.assertTrue(response.json["retryable"])
        self.assertIn("网关或后端服务暂时异常", response.json["error"])
        self.assertIn("a47bb9434cf6e670", response.json["error"])
        self.assertNotIn(self.payload["api_key"], response.get_data(as_text=True))

    def test_local_upstream_deadline_has_a_readable_timeout_message(self):
        with patch("app.probe_api", side_effect=APIError("The upstream request timed out", retryable=True)):
            response = self.client.post("/api/test/probe", json=self.payload)
        self.assertIn("60 秒", response.json["error"])
        self.assertTrue(response.json["retryable"])

    def test_manual_analysis_rejects_malformed_outputs(self):
        for outputs in (None, [], ["text"], [{"text": 123}], [{"text": "1", "expected_count": "many"}]):
            with self.subTest(outputs=outputs):
                response = self.client.post("/api/analyze", json={"outputs": outputs})
                self.assertEqual(response.status_code, 400)

    def test_invalid_temperature_returns_json_before_any_api_call(self):
        for temperature in (10 ** 400, "1e10000", [], {}, True):
            with self.subTest(temperature=temperature), patch("app.probe_api") as probe:
                response = self.client.post("/api/test/probe", json={**self.payload, "temperature": temperature})
                self.assertEqual(response.status_code, 400)
                self.assertIsInstance(response.json["error"], str)
                probe.assert_not_called()

    def test_vercel_https_proxy_accepts_same_site_origin(self):
        script = '''import json
from unittest.mock import patch
from app import app
payload = {"base_url":"https://api.example.com/v1", "api_key":"test-only",
           "api_model":"mock", "prompt":"Generate numbers", "expected_count":300}
with patch("app.probe_api", return_value={"accepted":False}):
    response = app.test_client().post("/api/test/probe", json=payload,
        base_url="http://modeltrace.example", headers={"Origin":"https://modeltrace.example",
        "X-Forwarded-Proto":"https"})
    print(json.dumps({"status":response.status_code, "body":response.json}))
'''
        environment = {**os.environ, "VERCEL": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        environment.pop("MODELTRACE_ALLOWED_HOSTS", None)
        process = subprocess.run([sys.executable, "-c", script], cwd=PROJECT, env=environment,
                                 check=True, capture_output=True, encoding="utf-8", timeout=15)
        result = json.loads(process.stdout)
        self.assertEqual(result["status"], 200, result)


if __name__ == "__main__":
    unittest.main()
