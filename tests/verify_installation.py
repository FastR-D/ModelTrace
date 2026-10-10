"""Verify wheels in a clean virtual environment outside the source tree."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import venv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def verify_installed(expected_sha, with_switcher):
    import modeltrace
    from modeltrace import fingerprint

    package = Path(modeltrace.__file__).resolve()
    assert package.is_relative_to(Path(sys.prefix).resolve()), package
    bank = fingerprint.DEFAULT_BANK.resolve(strict=True)
    assert bank.is_relative_to(package.parent), bank
    assert hashlib.sha256(bank.read_bytes()).hexdigest() == expected_sha
    model_ids = {item["id"] for item in modeltrace.load_bank()["models"]}
    assert model_ids
    assert importlib.util.find_spec("flask") is None, "Library installation must not require Flask"
    numbers = ",".join(str(index * 47 % 355 + 1) for index in range(330))

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/v1/messages":
                assert self.headers.get("x-api-key") == "installation-test-only"
                assert request["messages"][0]["content"]
                body = {"content": [{"type": "text", "text": numbers}], "stop_reason": "end_turn"}
            else:
                assert self.headers.get("Authorization") == "Bearer installation-test-only"
                if self.path == "/v1/responses":
                    assert request["input"]
                    body = {"status": "completed", "output": [{"type": "message", "content": [
                        {"type": "output_text", "text": numbers}]}]}
                else:
                    assert self.path == "/v1/chat/completions", self.path
                    assert request["messages"][0]["content"]
                    body = {"choices": [{"message": {"content": numbers}, "finish_reason": "stop"}]}
            self.server.seen.append(self.path)
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.seen = []
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}/v1"
    checks = []
    try:
        for protocol in ("openai", "responses", "anthropic"):
            before = len(server.seen)
            result = modeltrace.check_api(base, "installation-test-only", "mock", api_format=protocol,
                                          attempts=3, timeout=5, retries=0)
            assert result["used_outputs"] == 3
            assert {item["model"] for item in result["results"]} == model_ids
            assert len(server.seen) - before == 3
            checks.append(f"library-{protocol}")
        if with_switcher:
            import cc_simple
            import yaml

            assert Path(cc_simple.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
            binary = Path(sys.executable).with_name("cc-simple.exe" if os.name == "nt" else "cc-simple")
            source = Path.cwd() / "provider.yaml"
            environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

            def command(*arguments):
                process = subprocess.run([str(binary), "--json", "--config-dir", str(Path.cwd() / "store"),
                                          *arguments], capture_output=True, encoding="utf-8", env=environment,
                                         timeout=30, check=True)
                assert "installation-test-only" not in process.stdout + process.stderr
                return json.loads(process.stdout)

            for kind, protocol, target in (("openai", "responses", "codex"), ("anthropic", "anthropic", "claude")):
                source.write_text(yaml.safe_dump({"name": "installation-test", "type": kind, "base_url": base,
                                                 "api_key": "installation-test-only", "model_name": "mock"}),
                                  encoding="utf-8")
                result = command("check", str(source), "--timeout", "5", "--attempts", "3")
                assert result["analysis"]["api_test"]["api_format"] == protocol
                assert {item["model"] for item in result["analysis"]["results"]} == model_ids
                destination = Path.cwd() / target
                command("use", str(source), "--target-dir", str(destination), "--dry-run")
                assert not destination.exists()
                command("use", str(source), "--target-dir", str(destination))
                assert (destination / ("config.toml" if target == "codex" else "settings.json")).is_file()
                assert not (Path.cwd() / "store").exists()
                checks.append(f"cli-{target}")
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
    print(json.dumps({"status": "passed", "model_count": len(model_ids), "bank_sha256": expected_sha,
                      "checks": checks, "paid_api_requests": 0}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheels", nargs="+", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--installed", help=argparse.SUPPRESS)
    parser.add_argument("--switcher", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.installed:
        verify_installed(args.installed, args.switcher)
        return
    if not args.wheels:
        parser.error("--wheels is required")
    wheels = [path.resolve(strict=True) for path in args.wheels]
    project = Path(__file__).resolve().parents[1]
    expected_sha = hashlib.sha256((project / "data/unified_bank.json").read_bytes()).hexdigest()
    environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    with tempfile.TemporaryDirectory(prefix="modeltrace-install-") as temporary:
        root = Path(temporary).resolve()
        venv.EnvBuilder(with_pip=True).create(root / "env")
        binary = root / "env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        outside = root / "outside"
        outside.mkdir()
        install = [str(binary), "-X", "utf8", "-I", "-m", "pip", "install", "--disable-pip-version-check"]
        if args.wheelhouse:
            install.extend(["--no-index", "--find-links", str(args.wheelhouse.resolve(strict=True))])
        subprocess.run([*install, *map(str, wheels)], cwd=outside, env=environment, check=True, timeout=180)
        verify = [str(binary), "-X", "utf8", "-I", str(Path(__file__).resolve()), "--installed", expected_sha]
        if any(path.name.startswith("cc_switch_simple-") for path in wheels):
            verify.append("--switcher")
        subprocess.run(verify, cwd=outside, env=environment, check=True, timeout=60)


if __name__ == "__main__":
    main()
