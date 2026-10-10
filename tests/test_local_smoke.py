"""Exercise the local UI API and an OpenAI-compatible API without a paid key."""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from app import app, unified_bank


class MockModelHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        content_length = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(content_length))
        if payload.get("model") != "local-mock":
            self.send_error(400)
            return
        numbers = ", ".join(str((index * 47) % 355 + 1) for index in range(330))
        body = json.dumps(
            {"choices": [{"message": {"content": numbers}, "finish_reason": "stop"}]}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class LocalSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mock_server = ThreadingHTTPServer(("127.0.0.1", 0), MockModelHandler)
        cls.mock_thread = threading.Thread(target=cls.mock_server.serve_forever, daemon=True)
        cls.mock_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.mock_server.server_port}/v1"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.mock_server.shutdown()
        cls.mock_server.server_close()
        cls.mock_thread.join()

    def setUp(self) -> None:
        config = patch.dict(app.config, {"PUBLIC_MODE": False, "READ_ONLY": False})
        config.start()
        self.addCleanup(config.stop)
        self.client = app.test_client()

    def test_page_challenges_and_banks(self) -> None:
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("API 自动测试", page.get_data(as_text=True))
        challenges = self.client.get("/api/challenges").json["challenges"]
        self.assertEqual(len(challenges), 3)
        self.assertTrue(all(292 <= item["expected_count"] <= 332 for item in challenges))
        banks = self.client.get("/api/banks").json
        self.assertIn("gpt", banks)
        self.assertIn("claude", banks)

    def test_mock_api_probe_and_attribution(self) -> None:
        challenges = self.client.get("/api/challenges").json["challenges"]
        outputs = []
        for challenge in challenges:
            response = self.client.post(
                "/api/test/probe",
                json={
                    "base_url": self.base_url,
                    "api_key": "local-test-only",
                    "api_model": "local-mock",
                    "prompt": challenge["prompt"],
                    "expected_count": challenge["expected_count"],
                },
            )
            self.assertEqual(response.status_code, 200, response.json)
            self.assertTrue(response.json["accepted"])
            outputs.append(
                {"text": response.json["text"], "expected_count": challenge["expected_count"]}
            )
        result = self.client.post("/api/analyze", json={"outputs": outputs})
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json["used_outputs"], 3)
        self.assertEqual({item["model"] for item in result.json["results"]},
                         {item["id"] for item in unified_bank["models"]})
        self.assertAlmostEqual(sum(item["probability"] for item in result.json["results"]), 1)


if __name__ == "__main__":
    unittest.main()
