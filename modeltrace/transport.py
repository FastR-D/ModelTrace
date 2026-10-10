"""Small HTTP transports with bounded retries, token redaction and no redirects."""

from __future__ import annotations

import http.client
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit, urlunsplit

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
FORMATS = {"auto", "openai", "responses", "anthropic"}
_DNS_SLOTS = threading.BoundedSemaphore(8)
DEFAULT_UPSTREAM_USER_AGENT = (
    "Codex Desktop/0.147.0-alpha.1.2 (Windows 10.0.26200; x86_64) unknown "
    "(codex_exec; 0.147.0-alpha.1.2)"
)


class APIError(RuntimeError):
    def __init__(self, message, *, status_code=None, retryable=False, protocol_mismatch=False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.protocol_mismatch = protocol_mismatch


def upstream_user_agent():
    return (os.environ.get("MODELTRACE_USER_AGENT") or os.environ.get("GPT56_USER_AGENT")
            or DEFAULT_UPSTREAM_USER_AGENT).strip()


def _redact(message, api_key):
    text = str(message).replace(api_key, "[redacted]") if api_key else str(message)
    text = re.sub(r"(?i)(bearer\s+)[^\s\"'<>]+", r"\1[redacted]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|authorization|token)\s*[=:]\s*)[^\s,}\"']+",
                  r"\1[redacted]", text)
    return text[:500]


def _error_message(body, api_key):
    text = body.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
        error = payload.get("error", payload) if isinstance(payload, dict) else payload
        if isinstance(error, dict):
            text = error.get("message") or error.get("detail") or error.get("title") or str(error)
            ray_id = error.get("ray_id")
            if isinstance(ray_id, str) and re.fullmatch(r"[a-fA-F0-9]{16,32}", ray_id):
                text = f"{text} (Ray ID: {ray_id})"
        else:
            text = str(error)
    except (ValueError, TypeError):
        if any(marker in text.lower() for marker in ("cloudflare", "<html", "<!doctype")):
            text = "The upstream gateway rejected the API request (WAF or HTML page)."
    return _redact(str(text).strip() or "Upstream request failed", api_key)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise APIError("The upstream request timed out", retryable=True)
    return remaining


def _resolve_dns(hostname, port, deadline):
    # libc DNS has no timeout parameter. A daemon job lets the caller honor its
    # deadline, and slots prevent stalled DNS lookups spawning unbounded threads.
    if not _DNS_SLOTS.acquire(timeout=_remaining(deadline)):
        raise APIError("The upstream DNS lookup timed out", retryable=True)
    completed, outcome = threading.Event(), []

    def resolve():
        try:
            outcome.append(socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM))
        except OSError:
            outcome.append(None)
        finally:
            _DNS_SLOTS.release()
            completed.set()

    resolver = threading.Thread(target=resolve, daemon=True)
    try:
        resolver.start()
    except BaseException:
        _DNS_SLOTS.release()
        raise
    if not completed.wait(timeout=_remaining(deadline)):
        raise APIError("The upstream DNS lookup timed out", retryable=True)
    _remaining(deadline)
    if not outcome or outcome[0] is None:
        raise APIError("Could not resolve the upstream hostname", retryable=True)
    return list(dict.fromkeys(item[4][0] for item in outcome[0]))


def validate_url(base_url, *, public_only=False, deadline=None):
    """Validate a URL, returning parsed URL and optionally DNS-pinned addresses."""
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url is required")
    if any(ord(character) < 33 for character in base_url):
        raise ValueError("base_url contains whitespace or control characters")
    try:
        parsed = urlsplit(base_url.rstrip("/"))
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid API URL or port") from None
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("base_url must be an absolute HTTP(S) API URL")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("API URLs cannot contain credentials, query strings or fragments")
    if public_only and parsed.scheme != "https":
        raise ValueError("The public web service only accepts HTTPS API URLs")
    if not public_only:
        return parsed, None
    ips = _resolve_dns(parsed.hostname, port or 443,
                       time.monotonic() + 60 if deadline is None else deadline)
    if not ips or any(not ipaddress.ip_address(address).is_global for address in ips):
        raise ValueError("The public web service requires a publicly routable API host")
    return parsed, ips


def completion_url(base_url, api_format="openai"):
    parsed, _ = validate_url(base_url)
    endpoints = {"openai": "/chat/completions", "responses": "/responses", "anthropic": "/messages"}
    if api_format not in endpoints:
        raise ValueError("api_format must be openai, responses or anthropic")
    path = parsed.path.rstrip("/")
    for endpoint in endpoints.values():
        if path.endswith(endpoint):
            path = path[:-len(endpoint)]
            break
    # A supplied versioned/custom gateway prefix is already an API base.
    if not path:
        path = "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path + endpoints[api_format], "", ""))


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, address, timeout, *, deadline=None):
        super().__init__(host, port=port, timeout=timeout)
        self.addresses = [address] if isinstance(address, str) else list(address)
        self.address = self.addresses[0]
        self.deadline = time.monotonic() + timeout if deadline is None else deadline

    def connect(self):
        for index, address in enumerate(self.addresses):
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            self.sock = None
            try:
                self.sock = socket.socket(family, socket.SOCK_STREAM)
                # Reserve a share for remaining IPs when a dual-stack host's
                # first route is unreachable. No request has been sent yet.
                self.sock.settimeout(_remaining(self.deadline) / (len(self.addresses) - index))
                self.sock.connect((address, self.port))
                self.sock.settimeout(_remaining(self.deadline))
                self.address = address
                return
            except OSError:
                if self.sock:
                    self.sock.close()
                if index + 1 == len(self.addresses):
                    raise
            except BaseException:
                if self.sock:
                    self.sock.close()
                raise


class _PinnedHTTPSConnection(_PinnedHTTPConnection):
    default_port = 443

    def __init__(self, host, port, address, timeout, *, deadline=None):
        super().__init__(host, port, address, timeout, deadline=deadline)
        self._context = ssl.create_default_context()
        self._context.set_alpn_protocols(["http/1.1"])

    def connect(self):
        super().connect()
        raw_socket = self.sock
        try:
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host,
                                                   do_handshake_on_connect=False)
            self.sock.settimeout(_remaining(self.deadline))
            self.sock.do_handshake()
            self.sock.settimeout(_remaining(self.deadline))
        except BaseException:
            raw_socket.close()
            if self.sock:
                self.sock.close()
            raise


def _post(url, api_key, payload, headers, timeout, public_only):
    deadline = time.monotonic() + timeout
    parsed, addresses = validate_url(url, public_only=public_only, deadline=deadline)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if addresses is None:
        addresses = _resolve_dns(parsed.hostname, port, deadline)
    if not addresses:
        raise APIError("Could not resolve the upstream hostname", retryable=True)
    connection_type = _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
    target = addresses[0] if len(addresses) == 1 else addresses
    connection = connection_type(parsed.hostname, port, target, _remaining(deadline), deadline=deadline)
    expired, active_sockets = threading.Event(), []

    def abort():
        expired.set()
        for active in [connection.sock, *active_sockets]:
            if active is not None:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    active.close()
                except OSError:
                    pass

    watchdog = threading.Timer(_remaining(deadline), abort)
    watchdog.daemon = True
    watchdog.start()
    try:
        connection.request("POST", parsed.path or "/", body=json.dumps(payload).encode("utf-8"), headers=headers)
        remaining = _remaining(deadline)
        if connection.sock:
            connection.sock.settimeout(remaining)
            active_sockets.append(connection.sock)
        response = connection.getresponse()
        _remaining(deadline)
        declared_size = response.getheader("Content-Length")
        if declared_size and int(declared_size) > MAX_RESPONSE_BYTES:
            raise APIError("Upstream response exceeds the 2 MiB limit")
        chunks, size = [], 0
        response_socket = connection.sock or getattr(getattr(response.fp, "raw", None), "_sock", None)
        if response_socket:
            active_sockets.append(response_socket)
        while True:
            remaining = _remaining(deadline)
            if response_socket:
                response_socket.settimeout(remaining)
            chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise APIError("Upstream response exceeds the 2 MiB limit")
            if response.isclosed():
                break
        body = b"".join(chunks)
        if response.status >= 300:
            if 300 <= response.status < 400:
                message = "API redirects are disabled to protect the API token"
            else:
                message = _error_message(body, api_key)
            mismatch = response.status in {404, 405, 415} or (
                response.status == 400 and any(marker in message.lower() for marker in (
                    "unsupported endpoint", "unsupported api", "unknown endpoint",
                    "unrecognized request", "messages is required", "input is required",
                    "missing required parameter: 'input'", "missing required parameter: 'messages'")))
            # A model-not-found error must not be mistaken for endpoint discovery.
            if any(marker in message.lower() for marker in ("model_not_found", "model not found", "model does not exist")):
                mismatch = False
            raise APIError(f"HTTP {response.status}: {message}", status_code=response.status,
                           retryable=response.status in RETRYABLE_STATUS, protocol_mismatch=mismatch)
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise APIError("The upstream returned invalid JSON") from None
        if not isinstance(decoded, dict):
            raise APIError("The upstream returned an unexpected JSON shape")
        return decoded
    except APIError:
        if expired.is_set():
            raise APIError("The upstream request timed out", retryable=True) from None
        raise
    except (TimeoutError, socket.timeout):
        raise APIError("The upstream request timed out", retryable=True) from None
    except (OSError, ssl.SSLError, http.client.HTTPException):
        if expired.is_set():
            raise APIError("The upstream request timed out", retryable=True) from None
        raise APIError("Could not connect to the upstream API", retryable=True) from None
    except ValueError:
        raise APIError("The upstream returned invalid HTTP metadata") from None
    finally:
        watchdog.cancel()
        connection.close()


def _payload(api_model, prompt, temperature, api_format, system_prompt, api_key):
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": upstream_user_agent()}
    if api_format == "anthropic":
        body = {"model": api_model, "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}]}
        if system_prompt:
            body["system"] = system_prompt
        headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    elif api_format == "responses":
        body = {"model": api_model, "input": prompt, "max_output_tokens": 4096, "store": False}
        if system_prompt:
            body["instructions"] = system_prompt
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        body = {"model": api_model, "messages": [
            *([{"role": "system", "content": system_prompt}] if system_prompt else []),
            {"role": "user", "content": prompt}]}
        headers["Authorization"] = f"Bearer {api_key}"
    if temperature is not None:
        body["temperature"] = temperature
    return body, headers


def _text(payload, api_format):
    try:
        if payload.get("error"):
            raise APIError("The upstream returned an error instead of a model response")
        if api_format == "anthropic":
            if payload.get("stop_reason") in {"refusal", "max_tokens"}:
                raise APIError("The model refused or truncated its response")
            text = "".join(block.get("text", "") for block in payload["content"] if block.get("type") == "text")
        elif api_format == "responses":
            if payload.get("status") in {"failed", "incomplete", "cancelled", "queued", "in_progress"}:
                raise APIError("The Responses request did not complete")
            blocks = [block for item in payload["output"] if item.get("type") == "message"
                      for block in item.get("content", [])]
            if any(block.get("type") == "refusal" for block in blocks):
                raise APIError("The model refused its response")
            text = "".join(block.get("text", "") for block in blocks if block.get("type") == "output_text")
        else:
            choice = payload["choices"][0]
            if choice.get("finish_reason") in {"length", "content_filter"} or choice["message"].get("refusal"):
                raise APIError("The model refused or truncated its response")
            text = choice["message"]["content"]
            if isinstance(text, list):
                text = "".join(part.get("text", "") for part in text if part.get("type") in {None, "text"})
        if not isinstance(text, str) or not text.strip():
            raise APIError("The upstream response contains no model text")
        return text
    except (KeyError, IndexError, TypeError, AttributeError):
        raise APIError(f"The upstream response is not valid {api_format} output") from None


def complete(base_url, api_key, api_model, prompt, temperature=None, api_format="auto",
             system_prompt="", *, timeout=60, retries=2, public_only=False):
    if api_format not in FORMATS:
        raise ValueError("api_format must be auto, openai, responses or anthropic")
    if not isinstance(api_key, str) or not api_key.strip() or "\n" in api_key or "\r" in api_key:
        raise ValueError("A nonempty API token is required")
    if not isinstance(api_model, str) or not api_model.strip():
        raise ValueError("api_model is required")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("A nonempty challenge prompt is required")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 240 or not math.isfinite(timeout):
        raise ValueError("timeout must be between 0 and 240 seconds")
    if not isinstance(retries, int) or isinstance(retries, bool) or not 0 <= retries <= 3:
        raise ValueError("retries must be an integer from 0 to 3")
    if temperature is not None and (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                                    or not 0 <= temperature <= 2 or not math.isfinite(temperature)):
        raise ValueError("temperature must be a finite number between 0 and 2")
    parsed, _ = validate_url(base_url)
    candidates = [api_format]
    if api_format == "auto":
        selected = next((name for name, suffix in (("responses", "/responses"), ("anthropic", "/messages"),
                                                   ("openai", "/chat/completions")) if parsed.path.endswith(suffix)), None)
        candidates = [selected] if selected else ["openai", "responses", "anthropic"]
    deadline = time.monotonic() + timeout
    for candidate_index, candidate in enumerate(candidates):
        body, headers = _payload(api_model, prompt, temperature, candidate, system_prompt, api_key)
        for attempt in range(retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise APIError("The upstream request timed out", retryable=True)
            try:
                payload = _post(completion_url(base_url, candidate), api_key, body, headers, remaining, public_only)
                return _text(payload, candidate), candidate
            except APIError as error:
                if error.protocol_mismatch and candidate_index + 1 < len(candidates):
                    break
                if error.retryable and attempt < retries:
                    delay = min(0.5 * (2 ** attempt), max(0, deadline - time.monotonic()))
                    time.sleep(delay)
                    continue
                raise
    raise APIError("No supported API protocol was found")


def request_completion(base_url, api_key, api_model, prompt, temperature=None,
                       api_format="auto", system_prompt="", *, timeout=60, retries=2, public_only=False):
    return complete(base_url, api_key, api_model, prompt, temperature, api_format,
                    system_prompt, timeout=timeout, retries=retries, public_only=public_only)[0]
