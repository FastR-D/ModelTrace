"""Installable ModelTrace API. Network calls are explicit and never persist tokens."""

from .transport import APIError

__version__ = "0.1.0"
__all__ = ["APIError", "analyze_outputs", "check_api", "generate_challenges", "load_bank", "probe_api"]


def load_bank(path=None):
    from .fingerprint import load_bank as load

    return load() if path is None else load(path)


def generate_challenges(count=3):
    from .fingerprint import generate_challenges as generate

    return generate(count)


def analyze_outputs(outputs, bank=None):
    """Analyze pasted outputs against the bundled candidate bank or a custom bank."""
    from .fingerprint import analyze_global_outputs

    result = analyze_global_outputs(outputs, load_bank() if bank is None else bank)
    result["families"] = result["family_probabilities"]
    return result


def probe_api(base_url, api_key, api_model, prompt, expected_count, temperature=None,
              api_format="auto", timeout=60, *, system_prompt="", retries=2, public_only=False):
    """Request one challenge; accepted means its parsed sequence meets the threshold."""
    from .client import probe_api as probe

    return probe(base_url, api_key, api_model, prompt, expected_count, temperature,
                 api_format, timeout, system_prompt=system_prompt, retries=retries,
                 public_only=public_only)


def check_api(base_url, api_key, api_model, api_format="auto", temperature=None,
              timeout=60, attempts=6, *, bank=None, retries=2, public_only=False):
    """Require three valid challenges before returning a calibrated attribution."""
    from .client import check_api as check

    return check(base_url, api_key, api_model, api_format, temperature, timeout,
                 attempts, bank=bank, retries=retries, public_only=public_only)
