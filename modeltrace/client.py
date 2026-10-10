"""Public API attribution workflow; short outputs never count as a completed check."""

import math

from .fingerprint import analyze_global_outputs, generate_challenges, load_bank, parse_numbers
from .transport import APIError, complete


def probe_api(base_url, api_key, api_model, prompt, expected_count, temperature=None,
              api_format="auto", timeout=60, *, system_prompt="", retries=2, public_only=False):
    if isinstance(expected_count, bool) or not isinstance(expected_count, int) or not 1 <= expected_count <= 10000:
        raise ValueError("expected_count must be an integer from 1 to 10000")
    text, actual_format = complete(base_url, api_key, api_model, prompt, temperature,
                                   api_format, system_prompt, timeout=timeout, retries=retries,
                                   public_only=public_only)
    parsed = len(parse_numbers(text))
    minimum = max(80, math.ceil(expected_count * 0.55))
    return {"text": text, "parsed_numbers": parsed, "minimum_numbers": minimum,
            "accepted": parsed >= minimum, "api_format": actual_format}


def check_api(base_url, api_key, api_model, api_format="auto", temperature=None,
              timeout=60, attempts=6, *, bank=None, retries=2, public_only=False):
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 3 <= attempts <= 40:
        raise ValueError("attempts must be an integer from 3 to 40")
    outputs, errors = [], []
    attempted = 0
    actual_format = api_format
    for challenge in generate_challenges(attempts):
        attempted += 1
        try:
            probe = probe_api(base_url, api_key, api_model, challenge["prompt"],
                              challenge["expected_count"], temperature, actual_format, timeout,
                              retries=retries, public_only=public_only)
            actual_format = probe["api_format"]
        except APIError as error:
            # Credentials, limits, unavailable endpoints and network failures require a fix,
            # not another billable challenge. A refused/truncated answer can be replaced.
            if error.status_code is not None or error.retryable:
                raise
            errors.append(str(error))
            continue
        if probe["accepted"]:
            outputs.append({"text": probe["text"], "expected_count": challenge["expected_count"]})
        else:
            errors.append(f"有效数字不足：{probe['parsed_numbers']}/{probe['minimum_numbers']}")
        if len(outputs) == 3:
            break
    if len(outputs) < 3:
        details = "；".join(errors)
        raise APIError(f"未获得3条有效回答：收到 {len(outputs)}/3，已尝试 {attempted}/{attempts}。{details}")
    result = analyze_global_outputs(outputs, load_bank() if bank is None else bank)
    result["families"] = result["family_probabilities"]
    result["api_test"] = {"requested": 3, "attempted": attempted, "max_attempts": attempts,
                          "received": len(outputs), "errors": errors, "api_format": actual_format}
    return result
