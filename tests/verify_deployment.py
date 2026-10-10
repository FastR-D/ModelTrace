"""Verify a public deployment without credentials or paid model requests."""

import argparse
import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    parser.add_argument("--output", default="output/online-verification.json")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    project = Path(__file__).resolve().parents[1]
    local_bank = json.loads((project / "data/unified_bank.json").read_text(encoding="utf-8"))
    model_ids = {model["id"] for model in local_bank["models"]}
    checks = []

    def request(path, payload=None, expected=200):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"User-Agent": "ModelTrace-Deployment-Verification/1.0"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        query = urllib.request.Request(base + path, data=body, headers=headers)
        try:
            response = urllib.request.urlopen(query, timeout=45)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read()
            status = response.status
            cache = response.headers.get("Cache-Control", "")
        assert status == expected, f"{path}: expected {expected}, got {status}: {content[:200]!r}"
        checks.append({"path": path, "status": status, "cache_control": cache})
        return content

    home = request("/").decode("utf-8")
    assert 'id="test-api" class="mode-panel active"' in home
    assert 'id="auto-enrollment"' not in home
    challenges = json.loads(request("/api/challenges"))["challenges"]
    assert len(challenges) == 3
    banks = json.loads(request("/api/banks"))
    assert sum(bank["model_count"] for bank in banks.values()) == len(model_ids)
    javascript = request("/static/app.js")
    local_script = project / "static/app.js"
    assert hashlib.sha256(javascript).digest() == hashlib.sha256(local_script.read_bytes()).digest()
    sequence = ", ".join(str(index * 47 % 355 + 1) for index in range(330))
    outputs = [{"text": sequence, "expected_count": challenge["expected_count"]} for challenge in challenges]
    analysis = json.loads(request("/api/analyze", {"outputs": outputs}))
    assert analysis["used_outputs"] == 3
    assert {item["model"] for item in analysis["results"]} == model_ids
    assert abs(sum(item["probability"] for item in analysis["results"]) - 1) < 1e-9
    request("/api/banks", {}, 403)
    request("/api/enroll/auto", {}, 403)
    request("/api/test/auto", {}, 409)
    blocked = json.loads(request("/api/test/probe", {
        "base_url": "https://127.0.0.1/v1", "api_key": "verification-placeholder",
        "api_model": "mock", "prompt": "Generate integers", "expected_count": 300,
    }, 400))
    assert "verification-placeholder" not in json.dumps(blocked)
    report = {"base_url": base, "model_count": len(model_ids), "used_outputs": 3,
              "paid_api_requests": 0, "checks": checks, "status": "passed"}
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
