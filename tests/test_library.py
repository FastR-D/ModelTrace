"""Local HTTP contracts: no paid tokens and no mutation of reference data."""

import json
import importlib.util
import shutil
import socket
import ssl
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from modeltrace import APIError, analyze_outputs, check_api, load_bank, probe_api
from modeltrace import transport

SEQUENCE = ", ".join(str((index * 47) % 355 + 1) for index in range(330))


def output_for(api_format, text=SEQUENCE):
    if api_format == "anthropic":
        return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}
    if api_format == "responses":
        return {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": text}]}]}
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append((self.path, dict(self.headers), body))
        status, payload, extra = self.server.replies.pop(0)
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class LibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.server.seen = []
        self.server.replies = []

    def reply(self, payload=None, status=200, headers=None):
        self.server.replies.append((status, output_for("openai") if payload is None else payload, headers or {}))

    def probe(self, api_format="auto", **kwargs):
        return probe_api(self.base_url, "test-secret-token", "local-mock", "Return numbers", 300,
                         api_format=api_format, retries=0, **kwargs)

    def test_default_bank_and_original_imports(self):
        import enrollment
        import fingerprint
        from modeltrace import enrollment as installed_enrollment
        from modeltrace import fingerprint as installed_fingerprint

        self.assertIs(enrollment, installed_enrollment)
        self.assertIs(fingerprint, installed_fingerprint)
        project = Path(__file__).resolve().parents[1]
        expected = json.loads((project / "data" / "unified_bank.json").read_text(encoding="utf-8"))
        self.assertGreater(len(expected["models"]), 0)
        self.assertEqual(load_bank(), expected)
        self.assertEqual(installed_enrollment.DATA_FILE, project / "data" / "gpt_reference.jsonl")
        self.assertEqual(fingerprint.DEFAULT_BANK, project / "data" / "unified_bank.json")

    def test_packaged_bank_wins_over_unrelated_parent_data(self):
        from modeltrace import fingerprint

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "modeltrace"
            (package / "data").mkdir(parents=True)
            (root / "data").mkdir()
            source = package / "fingerprint.py"
            shutil.copyfile(fingerprint.__file__, source)
            shutil.copyfile(fingerprint.DEFAULT_BANK, package / "data" / "unified_bank.json")
            (root / "data" / "unified_bank.json").write_text('{"models": []}', encoding="utf-8")
            spec = importlib.util.spec_from_file_location("isolated_fingerprint", source)
            isolated = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(isolated)
            self.assertEqual(isolated.load_bank(), load_bank())

    def test_three_protocols_and_headers(self):
        for api_format, endpoint in (("openai", "chat/completions"), ("responses", "responses"),
                                     ("anthropic", "messages")):
            with self.subTest(api_format=api_format):
                self.reply(output_for(api_format))
                result = self.probe(api_format)
                path, headers, body = self.server.seen[-1]
                self.assertEqual(path, "/v1/" + endpoint)
                self.assertTrue(result["accepted"])
                self.assertEqual(result["parsed_numbers"], 330)
                self.assertEqual(result["api_format"], api_format)
                self.assertNotIn("temperature", body)
                if api_format == "anthropic":
                    self.assertEqual(headers["x-api-key"], "test-secret-token")
                    self.assertNotIn("Authorization", headers)
                    self.assertEqual(headers["anthropic-version"], "2023-06-01")
                else:
                    self.assertEqual(headers["Authorization"], "Bearer test-secret-token")
                if api_format == "responses":
                    self.assertFalse(body["store"])

    def test_auto_fallback_and_cache(self):
        self.reply({"error": "unknown endpoint"}, 404)
        self.reply(output_for("responses"))
        self.reply(output_for("responses"))
        self.reply(output_for("responses"))
        result = check_api(self.base_url, "test-secret-token", "local-mock", retries=0)
        self.assertEqual([item[0] for item in self.server.seen], [
            "/v1/chat/completions", "/v1/responses", "/v1/responses", "/v1/responses"])
        self.assertEqual(result["used_outputs"], 3)
        self.assertEqual({item["model"] for item in result["results"]},
                         {item["id"] for item in load_bank()["models"]})
        self.assertEqual(result["api_test"]["api_format"], "responses")

    def test_auto_does_not_switch_after_auth_rate_limit_model_error(self):
        for status, error in ((401, "invalid test-secret-token"), (429, "rate limit"),
                              (400, "model not found"), (404, "model_not_found")):
            with self.subTest(status=status):
                self.server.seen = []
                self.reply({"error": {"message": error}}, status)
                with self.assertRaises(APIError) as context:
                    self.probe()
                self.assertEqual(context.exception.status_code, status)
                self.assertNotIn("test-secret-token", str(context.exception))
                self.assertEqual(len(self.server.seen), 1)

    def test_retry_stays_on_same_protocol(self):
        self.reply({"error": "try later"}, 429)
        self.reply()
        with patch("modeltrace.transport.time.sleep"):
            result = probe_api(self.base_url, "test-secret-token", "local-mock", "Numbers", 300,
                               retries=1)
        self.assertTrue(result["accepted"])
        self.assertEqual([item[0] for item in self.server.seen], ["/v1/chat/completions"] * 2)

    def test_gateway_problem_json_extracts_detail_ray_id_and_redacts_token(self):
        self.reply({"type": "https://developers.cloudflare.com/502", "title": "Error 502: Bad gateway",
                    "status": 502, "detail": "The origin returned an incomplete response test-secret-token",
                    "error_name": "origin_bad_gateway", "ray_id": "a47bb9434cf6e670"}, 502)
        with self.assertRaises(APIError) as context:
            self.probe()
        error = context.exception
        self.assertEqual(error.status_code, 502)
        self.assertTrue(error.retryable)
        self.assertIn("incomplete response", str(error))
        self.assertIn("Ray ID: a47bb9434cf6e670", str(error))
        self.assertNotIn("test-secret-token", str(error))
        self.assertNotIn("{'type'", str(error))
        self.assertEqual(len(self.server.seen), 1)

    def test_timeout_retries_do_not_change_protocol(self):
        with patch("modeltrace.transport._post", side_effect=APIError("timed out", retryable=True)) as post:
            with patch("modeltrace.transport.time.sleep"):
                with self.assertRaises(APIError):
                    probe_api(self.base_url, "test-secret-token", "local-mock", "Numbers", 300, retries=1)
        self.assertEqual(post.call_count, 2)
        self.assertTrue(all(call.args[0].endswith("/chat/completions") for call in post.call_args_list))

    def test_three_valid_outputs_are_required(self):
        for valid_count in (0, 1, 2):
            with self.subTest(valid_count=valid_count):
                for index in range(3):
                    self.reply(output_for("openai", SEQUENCE if index < valid_count else "1 2 3"))
                with self.assertRaisesRegex(APIError, f"收到 {valid_count}/3"):
                    check_api(self.base_url, "test-secret-token", "local-mock", attempts=3, retries=0)
        for _ in range(3):
            self.reply()
        result = check_api(self.base_url, "test-secret-token", "local-mock", attempts=3, retries=0)
        self.assertEqual(result["api_test"]["received"], 3)
        self.assertAlmostEqual(sum(item["probability"] for item in result["results"]), 1)
        self.assertEqual(result["families"], result["family_probabilities"])

    def test_short_probe_and_manual_analysis(self):
        self.reply(output_for("openai", "1 2 3"))
        result = self.probe()
        self.assertFalse(result["accepted"])
        self.assertEqual(result["minimum_numbers"], 165)
        analysis = analyze_outputs([{"text": SEQUENCE, "expected_count": 300}])
        self.assertEqual(analysis["used_outputs"], 1)

    def test_redirect_does_not_forward_token(self):
        self.reply({}, 307, {"Location": self.base_url + "/sink"})
        with self.assertRaisesRegex(APIError, "redirects are disabled"):
            self.probe()
        self.assertEqual(len(self.server.seen), 1)

    def test_size_invalid_json_and_truncated_outputs(self):
        cases = [(b"x" * (transport.MAX_RESPONSE_BYTES + 1), "2 MiB"),
                 (b"not-json", "invalid JSON"),
                 ({"choices": [{"message": {"content": SEQUENCE}, "finish_reason": "length"}]}, "truncated"),
                 ({"status": "incomplete", "output": []}, "did not complete")]
        for payload, message in cases:
            with self.subTest(message=message):
                self.reply(payload)
                with self.assertRaisesRegex(APIError, message):
                    self.probe("responses" if message == "did not complete" else "openai")

    def test_endpoint_normalization(self):
        for base in ("https://example.com", "https://example.com/v1", "https://example.com/v1/messages"):
            self.assertEqual(transport.completion_url(base, "responses"), "https://example.com/v1/responses")
        self.assertEqual(transport.completion_url("https://example.com/custom/v1", "anthropic"),
                         "https://example.com/custom/v1/messages")

    def test_public_url_policy_blocks_private_dns_and_bad_urls(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch("modeltrace.transport.socket.getaddrinfo", return_value=addresses):
            with self.assertRaisesRegex(ValueError, "publicly routable"):
                transport.validate_url("https://example.com/v1", public_only=True)
        mixed = addresses + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with patch("modeltrace.transport.socket.getaddrinfo", return_value=mixed):
            with self.assertRaises(ValueError):
                transport.validate_url("https://example.com", public_only=True)
        for url in ("http://example.com", "https://user:pass@example.com", "https://example.com?token=x",
                    "https://example.com#fragment", "file:///tmp/key"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                transport.validate_url(url, public_only=True)

    def test_public_connection_pins_ip_and_preserves_tls_hostname(self):
        raw = MagicMock()
        tls = MagicMock()
        with patch("modeltrace.transport.socket.socket", return_value=raw) as create:
            connection = transport._PinnedHTTPSConnection("api.example.com", 443, "8.8.8.8", 10)
            connection._context = tls
            connection.connect()
        create.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
        raw.connect.assert_called_once_with(("8.8.8.8", 443))
        tls.wrap_socket.assert_called_once_with(raw, server_hostname="api.example.com", do_handshake_on_connect=False)
        tls.wrap_socket.return_value.do_handshake.assert_called_once_with()

    def test_public_post_uses_verified_address_without_second_dns_lookup(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        response = MagicMock()
        response.status = 200
        response.getheader.return_value = None
        response.read1.return_value = b'{}'
        response.isclosed.return_value = True
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch("modeltrace.transport.socket.getaddrinfo", return_value=addresses) as resolve:
            with patch("modeltrace.transport._PinnedHTTPSConnection", return_value=connection) as pinned:
                transport._post("https://api.example.com/v1/messages", "secret", {}, {}, 10, True)
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(pinned.call_args.args[:3], ("api.example.com", 443, "8.8.8.8"))

    def test_protocol_fallback_shares_one_total_timeout(self):
        mismatch = APIError("not found", status_code=404, protocol_mismatch=True)
        with patch("modeltrace.transport.time.monotonic", side_effect=[0, 10, 20]):
            with patch("modeltrace.transport._post", side_effect=[mismatch, output_for("responses")]) as post:
                text, api_format = transport.complete(self.base_url, "secret", "model", "Numbers", timeout=60, retries=0)
        self.assertEqual(text, SEQUENCE)
        self.assertEqual(api_format, "responses")
        self.assertEqual([call.args[4] for call in post.call_args_list], [50, 40])

    def test_slow_headers_and_body_cannot_reset_absolute_deadline(self):
        class SlowHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                try:
                    self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                    if self.path.startswith("/headers"):
                        for index in range(12):
                            time.sleep(0.03)
                            self.wfile.write(f"X-Slow-{index}: drip\r\n".encode())
                        self.wfile.write(b"Content-Length: 2\r\n\r\n{}")
                    else:
                        self.wfile.write(b"Content-Length: 12\r\n\r\n")
                        for _ in range(12):
                            time.sleep(0.03)
                            self.wfile.write(b"x")
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for phase in ("headers", "body"):
                with self.subTest(phase=phase):
                    start = time.monotonic()
                    with self.assertRaisesRegex(APIError, "timed out"):
                        transport.complete(f"http://127.0.0.1:{server.server_port}/{phase}",
                                           "secret", "model", "Numbers", timeout=0.12, retries=0)
                    self.assertLess(time.monotonic() - start, 0.25)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_slow_dns_cannot_exceed_absolute_deadline(self):
        def slow_dns(*args, **kwargs):
            time.sleep(0.3)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

        start = time.monotonic()
        with patch("modeltrace.transport.socket.getaddrinfo", side_effect=slow_dns):
            with self.assertRaisesRegex(APIError, "DNS lookup timed out"):
                transport.complete("https://api.example.com", "secret", "model", "Numbers",
                                   timeout=0.06, retries=0, public_only=True)
        self.assertLess(time.monotonic() - start, 0.2)

    def test_slow_tls_handshake_cannot_exceed_absolute_deadline(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(2)
        finished = threading.Event()
        connected = threading.Event()
        context = ssl.create_default_context()

        def silent_tls_peer():
            try:
                peer, _ = listener.accept()
            except OSError:
                return
            connected.set()
            try:
                finished.wait(2)
            finally:
                peer.close()

        thread = threading.Thread(target=silent_tls_peer, daemon=True)
        thread.start()
        try:
            start = time.monotonic()
            with patch("modeltrace.transport.ssl.create_default_context", return_value=context), self.assertRaisesRegex(APIError, "timed out"):
                transport.complete(f"https://127.0.0.1:{listener.getsockname()[1]}",
                                   "secret", "model", "Numbers", timeout=0.3, retries=0)
            self.assertTrue(connected.is_set(), "TLS timeout must reach the handshake")
            self.assertLess(time.monotonic() - start, 1)
        finally:
            finished.set()
            listener.close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def test_local_library_does_not_use_environment_proxy(self):
        self.reply()
        with patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1"}):
            self.assertTrue(self.probe()["accepted"])

    def test_localhost_dual_stack_tries_ipv4_after_ipv6_refusal(self):
        self.reply()
        base_url = f"http://localhost:{self.server.server_port}/v1"
        result = probe_api(base_url, "secret", "model", "Numbers", 300, timeout=2, retries=0)
        self.assertTrue(result["accepted"])
        self.assertEqual(len(self.server.seen), 1)

    def test_parameter_validation(self):
        for kwargs in ({"attempts": 2}, {"attempts": 41}, {"attempts": True}):
            with self.assertRaises(ValueError):
                check_api(self.base_url, "test-secret-token", "local-mock", **kwargs)
        for kwargs in ({"timeout": float("inf")}, {"temperature": float("nan")}, {"timeout": 0}):
            with self.assertRaises(ValueError):
                self.probe(**kwargs)

    def test_huge_numeric_parameters_are_validation_errors(self):
        for field in ("temperature", "timeout"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.probe(**{field: 10 ** 400})


if __name__ == "__main__":
    unittest.main()
