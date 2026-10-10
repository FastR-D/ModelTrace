from __future__ import annotations

import json
import math
import os
import re
import secrets
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge, UnsupportedMediaType
from werkzeug.middleware.proxy_fix import ProxyFix

from modeltrace import APIError, check_api, probe_api

from enrollment import bank_summary, enroll_automatic
from fingerprint import analyze_global_outputs, generate_challenges, load_bank
from bank_builder import build_bank, read_rows


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
app.config["PUBLIC_MODE"] = bool(os.getenv("VERCEL")) or os.getenv("MODELTRACE_PUBLIC") == "1"
app.config["READ_ONLY"] = app.config["PUBLIC_MODE"] or os.getenv("MODELTRACE_READ_ONLY") == "1"
if os.getenv("VERCEL"):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=1, x_host=0, x_port=0, x_prefix=0)
PROJECT = Path(__file__).resolve().parent
CUSTOM_BANKS_FILE = PROJECT / "data" / "custom_banks.json"
UNIFIED_BANK_FILE = PROJECT / "data" / "unified_bank.json"
DEFAULT_BANK_ID = "claude"


def builtin_configs() -> dict[str, dict]:
    return {
        "gpt": {
            "label": "GPT",
            "bank_file": PROJECT / "data" / "gpt_bank.json",
            "data_file": PROJECT / "data" / "gpt_reference.jsonl",
        },
        "claude": {
            "label": "Claude",
            "bank_file": PROJECT / "data" / "claude_bank.json",
            "data_file": PROJECT / "data" / "claude_reference.jsonl",
        },
    }


def load_configs() -> dict[str, dict]:
    configs = builtin_configs()
    if CUSTOM_BANKS_FILE.exists():
        for item in json.loads(CUSTOM_BANKS_FILE.read_text(encoding="utf-8")):
            bank_id = item["id"]
            configs[bank_id] = {
                "label": item["label"],
                "bank_file": PROJECT / "data" / f"{bank_id}_bank.json",
                "data_file": PROJECT / "data" / f"{bank_id}_reference.jsonl",
                "custom": True,
            }
    return configs


def save_custom_configs() -> None:
    items = [
        {"id": bank_id, "label": config["label"]}
        for bank_id, config in BANK_CONFIGS.items()
        if config.get("custom")
    ]
    CUSTOM_BANKS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_configured_bank(bank_id: str) -> dict | None:
    config = BANK_CONFIGS[bank_id]
    if not config["bank_file"].exists():
        return None
    bank = load_bank(config["bank_file"])
    bank["family_name"] = config["label"]
    return bank


BANK_CONFIGS = load_configs()
banks = {bank_id: load_configured_bank(bank_id) for bank_id in BANK_CONFIGS}


def active_banks() -> dict[str, dict]:
    return {
        bank_id: bank
        for bank_id, bank in banks.items()
        if bank is not None and bank.get("models")
    }


def global_reference_rows() -> list[dict]:
    rows = []
    for bank_id in active_banks():
        config = BANK_CONFIGS[bank_id]
        rows.extend(
            {
                **row,
                "family_id": bank_id,
                "family_name": config["label"],
            }
            for row in read_rows(config["data_file"])
        )
    return rows


def rebuild_global_bank() -> dict:
    global unified_bank
    unified_bank = build_bank(global_reference_rows())
    UNIFIED_BANK_FILE.write_text(
        json.dumps(unified_bank, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return unified_bank


unified_bank = load_bank(UNIFIED_BANK_FILE) if UNIFIED_BANK_FILE.exists() else None
if unified_bank is None:
    if app.config["READ_ONLY"]:
        raise RuntimeError("部署包缺少 data/unified_bank.json，请先在本地重建指纹库")
    rebuild_global_bank()


@app.before_request
def protect_cloud_routes():
    if app.config["READ_ONLY"] and request.method == "POST" and request.path in {"/api/banks", "/api/enroll/auto"}:
        return jsonify({"error": "云端指纹库只读；请在本地采集后重新部署"}), 403
    if app.config["PUBLIC_MODE"] and request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            return jsonify({"error": "仅允许本站发起请求"}), 403


@app.after_request
def response_headers(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.errorhandler(BadRequest)
@app.errorhandler(UnsupportedMediaType)
@app.errorhandler(RequestEntityTooLarge)
def invalid_request(error):
    messages = {400: "请求必须包含有效 JSON", 415: "请使用 application/json", 413: "请求内容过大"}
    return jsonify({"error": messages[error.code]}), error.code


@app.errorhandler(ValueError)
def invalid_value(error):
    return jsonify({"error": str(error)}), 400


def json_payload() -> dict:
    payload = request.get_json()
    if not isinstance(payload, dict):
        raise ValueError("请求内容必须是 JSON 对象")
    return payload


def api_configuration(payload: dict) -> dict:
    result = {}
    for field in ("base_url", "api_key", "api_model"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 4096:
            raise ValueError(f"请填写有效的 {field}")
        result[field] = value.strip()
    result["api_format"] = payload.get("api_format") or "auto"
    if result["api_format"] not in {"auto", "openai", "anthropic", "responses"}:
        raise ValueError("不支持的接口协议")
    result["temperature"] = requested_temperature(payload)
    result["timeout"] = 60
    result["public_only"] = app.config["PUBLIC_MODE"]
    allowed = {item.strip().lower() for item in os.getenv("MODELTRACE_ALLOWED_HOSTS", "").split(",") if item.strip()}
    if app.config["PUBLIC_MODE"] and allowed:
        from urllib.parse import urlsplit
        if (urlsplit(result["base_url"]).hostname or "").lower() not in allowed:
            raise ValueError("该接口域名不在部署者允许的列表中")
    return result


def upstream_failure(error, payload):
    message = str(error)
    token = payload.get("api_key") if isinstance(payload, dict) else None
    if isinstance(token, str) and token:
        message = message.replace(token, "[已隐藏密钥]")
    upstream_status = getattr(error, "status_code", None)
    detail = message
    if "timed out" in message.lower():
        message = "上游接口未在 60 秒内返回完整回答，请稍后再试。"
    elif upstream_status in {502, 503, 504}:
        message = f"上游接口返回 HTTP {upstream_status}，网关或后端服务暂时异常，请稍后再试。"
        ray_id = re.search(r"Ray ID: ([a-fA-F0-9]{16,32})", detail)
        if ray_id:
            message += f" Ray ID: {ray_id.group(1)}"
    status = 504 if upstream_status == 504 else 502
    return jsonify({"error": message, "upstream_status": upstream_status,
                    "upstream_detail": detail[:500], "retryable": getattr(error, "retryable", False)}), status


def requested_bank_id(payload: dict | None = None) -> str:
    bank_id = (payload or {}).get("bank_id") or request.args.get("bank_id") or DEFAULT_BANK_ID
    if not isinstance(bank_id, str) or bank_id not in BANK_CONFIGS:
        raise ValueError(f"未知指纹库：{bank_id}")
    return bank_id


def requested_temperature(payload: dict) -> float | None:
    value = payload.get("temperature")
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("温度必须是 0 到 2 之间的数字")
    if isinstance(value, (int, float)) and not 0 <= value <= 2:
        raise ValueError("温度必须是 0 到 2 之间的数字")
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("温度必须是 0 到 2 之间的数字") from None
    if not math.isfinite(value) or not 0 <= value <= 2:
        raise ValueError("温度必须是 0 到 2 之间的数字")
    return value


def summarized_bank(bank_id: str) -> dict:
    config = BANK_CONFIGS[bank_id]
    bank = banks.get(bank_id)
    summary = bank_summary(bank) if bank is not None else {
        "model_count": 0,
        "response_count": 0,
        "number_count": 0,
        "models": [],
        "calibration": {},
    }
    summary.update({"id": bank_id, "label": config["label"]})
    return summary


def summarized_unified_bank() -> dict:
    summaries = {bank_id: summarized_bank(bank_id) for bank_id in active_banks()}
    return {
        "id": "unified",
        "label": "全部指纹",
        "model_count": sum(item["model_count"] for item in summaries.values()),
        "response_count": sum(item["response_count"] for item in summaries.values()),
        "family_count": len(summaries),
        "families": [
            {"id": bank_id, "label": item["label"], "model_count": item["model_count"]}
            for bank_id, item in summaries.items()
        ],
    }


def replace_bank(bank_id: str) -> None:
    banks[bank_id] = load_configured_bank(bank_id)
    rebuild_global_bank()


@app.get("/")
def index():
    summaries = {bank_id: summarized_bank(bank_id) for bank_id in BANK_CONFIGS}
    return render_template(
        "index.html",
        banks=summaries,
        bank=summaries[DEFAULT_BANK_ID],
        unified=summarized_unified_bank(),
        default_bank_id=DEFAULT_BANK_ID,
        read_only=app.config["READ_ONLY"],
        public_mode=app.config["PUBLIC_MODE"],
    )


@app.get("/api/challenges")
def challenges():
    return jsonify({"challenges": generate_challenges(3)})


@app.post("/api/analyze")
def analyze():
    try:
        payload = json_payload()
        outputs = payload.get("outputs")
        if not isinstance(outputs, list) or not 1 <= len(outputs) <= 6:
            raise ValueError("请提交 1 到 6 份回答")
        for output in outputs:
            if not isinstance(output, dict) or not isinstance(output.get("text"), str):
                raise ValueError("每份回答必须包含 text 文本")
            expected = output.get("expected_count", 0)
            if isinstance(expected, bool) or not isinstance(expected, int) or not 0 <= expected <= 1000:
                raise ValueError("expected_count 必须是 0 到 1000 之间的整数")
        result = analyze_global_outputs(payload["outputs"], unified_bank)
        result["bank"] = summarized_unified_bank()
        return jsonify(result)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@app.post("/api/test/auto")
def automatic_test():
    payload = {}
    try:
        if app.config["PUBLIC_MODE"]:
            return jsonify({"error": "云端请逐条调用 /api/test/probe，再调用 /api/analyze；网页已自动处理"}), 409
        payload = json_payload()
        result = check_api(**api_configuration(payload), bank=unified_bank)
        result["bank"] = summarized_unified_bank()
        return jsonify(result)
    except (ValueError, TypeError) as error:
        return jsonify({"error": str(error)}), 400
    except APIError as error:
        return upstream_failure(error, payload)


@app.post("/api/test/probe")
def automatic_test_probe():
    payload = {}
    try:
        payload = json_payload()
        config = api_configuration(payload)
        prompt = payload.get("prompt")
        expected = payload.get("expected_count")
        if not isinstance(prompt, str) or not 1 <= len(prompt) <= 10000:
            raise ValueError("prompt 必须是 1 到 10000 字符的文本")
        if isinstance(expected, bool) or not isinstance(expected, int) or not 80 <= expected <= 1000:
            raise ValueError("expected_count 必须是 80 到 1000 之间的整数")
        return jsonify(probe_api(**config, prompt=prompt, expected_count=expected, retries=0))
    except (ValueError, TypeError) as error:
        return jsonify({"error": str(error)}), 400
    except APIError as error:
        return upstream_failure(error, payload)


@app.get("/api/bank")
def get_bank():
    try:
        return jsonify(summarized_bank(requested_bank_id()))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@app.get("/api/banks")
def get_banks():
    return jsonify({bank_id: summarized_bank(bank_id) for bank_id in BANK_CONFIGS})


@app.post("/api/banks")
def create_bank():
    payload = json_payload()
    label = payload.get("label")
    if not isinstance(label, str) or len(label) > 100:
        raise ValueError("指纹库名称必须是 1 到 100 字符的文本")
    label = label.strip()
    if not label:
        return jsonify({"error": "请输入指纹库名称"}), 400
    bank_id = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or f"bank-{secrets.token_hex(3)}"
    if bank_id in BANK_CONFIGS:
        return jsonify({"error": "同名指纹库已存在"}), 400
    config = {
        "label": label,
        "bank_file": PROJECT / "data" / f"{bank_id}_bank.json",
        "data_file": PROJECT / "data" / f"{bank_id}_reference.jsonl",
        "custom": True,
    }
    config["data_file"].parent.mkdir(parents=True, exist_ok=True)
    config["data_file"].touch()
    BANK_CONFIGS[bank_id] = config
    banks[bank_id] = None
    save_custom_configs()
    return jsonify(
        {
            "bank": summarized_bank(bank_id),
            "banks": {item: summarized_bank(item) for item in BANK_CONFIGS},
            "unified": summarized_unified_bank(),
        }
    )


@app.post("/api/enroll/auto")
def automatic_enrollment():
    payload = json_payload()
    try:
        api_config = api_configuration(payload)
        model_label = payload.get("model_label")
        if not isinstance(model_label, str) or not model_label.strip() or len(model_label) > 200:
            raise ValueError("请填写有效的指纹名称")
        bank_id = requested_bank_id(payload)
        config = BANK_CONFIGS[bank_id]
        result = enroll_automatic(
            base_url=api_config["base_url"],
            api_key=api_config["api_key"],
            api_model=api_config["api_model"],
            model_label=model_label.strip(),
            sample_count=int(payload.get("sample_count", 36)),
            temperature=requested_temperature(payload),
            api_format=api_config["api_format"],
            data_file=config["data_file"],
            bank_file=config["bank_file"],
            bank_id=bank_id,
            provider="api",
        )
        replace_bank(bank_id)
        result["bank"] = summarized_bank(bank_id)
        result["unified"] = summarized_unified_bank()
        return jsonify(result)
    except (RuntimeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=7860, debug=False)
