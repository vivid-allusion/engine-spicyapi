"""HTTP transport for the Spicy API — stdlib only.

One JSON client and one downloader. Every wire call the Engine makes goes
through here, so retry hints (``Retry-After``), provider error bodies and
error classification live in exactly one place.

The Spicy API is plain REST (OpenAI-compatible where it needs to be), so the
Engine carries no third-party SDK dependency: ``urllib.request`` is enough
for JSON in / JSON out and for pulling CDN outputs down.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.spicyapi.com"
DEFAULT_TIMEOUT = 600.0
DOWNLOAD_TIMEOUT = 300.0
RETRY_AFTER_CAP = 300.0
USER_AGENT = "engine-spicyapi/0.1 (+https://github.com/vivid-allusion/engine-spicyapi)"

_MAX_ERROR_CHARS = 600


class ProviderError(RuntimeError):
    """A non-2xx (or unreadable) response from the Spicy API.

    Attributes:
        status: HTTP status code, or 0 when the request never completed
            (connection error, timeout, bad JSON).
        payload: the decoded error body when there was one.
        retry_after: the ``Retry-After`` header in seconds, when sent.
    """

    def __init__(
        self,
        status: int,
        message: str,
        payload: dict | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}
        self.retry_after = retry_after

    @property
    def code(self) -> str:
        """Provider error code / type, when the body carries one."""
        error = self.payload.get("error")
        if isinstance(error, dict):
            for key in ("code", "type"):
                if error.get(key):
                    return str(error[key])
            return ""
        if isinstance(error, str):
            return error
        code = self.payload.get("code")
        return str(code) if code else ""


def error_message(payload: dict) -> str:
    """Human message out of the API's error envelope.

    ``{"error": {"message": ..., "type": ...}}`` on ``/v1/*``,
    ``{"error": "unauthorized", "message": "..."}`` on the tool surface.
    """
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("message", "detail", "code"):
            if error.get(key):
                return str(error[key])
    if isinstance(error, str) and error:
        if payload.get("message"):
            return f"{error}: {payload['message']}"
        return error
    if payload.get("message"):
        return str(payload["message"])
    return ""


def _is_fatal(exc: Exception) -> bool:
    """Billing / auth / balance failures: re-running will not help."""
    status = getattr(exc, "status", None)
    if status in (401, 402, 403):
        return True
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "insufficient balance",
            "insufficient_balance",
            "insufficient",
            "billing",
            "balance",
            "unauthorized",
            "invalid api key",
            "reference_terms_required",
            "end_user_suspended",
            "forbidden",
        )
    )


def is_high_demand(exc: Exception) -> bool:
    """503 'model at capacity' / upstream-unavailable spikes."""
    status = getattr(exc, "status", None)
    if status == 503:
        return True
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "high demand",
            "currently unavailable",
            "service unavailable",
            "upstream",
            "503",
        )
    )


def classify_error(exc: Exception) -> str:
    """Classify a provider exception.

    Returns one of ``"retryable"`` (rate limit, capacity — retrying helps),
    ``"fatal"`` (billing, auth, balance — the whole batch will fail) or
    ``"other"`` (bad parameters, moderation declines — per-file only, the
    next Markdown file may well succeed).
    """
    status = getattr(exc, "status", None)
    if _is_fatal(exc):
        return "fatal"
    if status == 429 or is_high_demand(exc):
        return "retryable"
    text = str(exc).lower()
    if any(token in text for token in ("rate limit", "ratelimit", "throttl", "too many")):
        return "retryable"
    if status and 500 <= status < 600:
        return "retryable"
    return "other"


def is_moderation_decline(exc: Exception) -> bool:
    """A 422: the prompt was blocked by the screen. Nothing was charged."""
    return getattr(exc, "status", None) == 422


def is_batch_fatal(exc: Exception) -> bool:
    """True when every remaining Markdown file would fail the same way."""
    return classify_error(exc) == "fatal"


def backoff(
    base_delay: float, attempt: int, *, long_backoff: bool = False, exc: Exception | None = None
) -> float:
    """Seconds to wait before retry ``attempt`` (0-based).

    The provider's ``Retry-After`` wins when it sends one; otherwise this is
    exponential, stretched 4x for capacity outages, which resolve far slower
    than a plain rate-limit burst.
    """
    retry_after = getattr(exc, "retry_after", None)
    if retry_after:
        return max(1.0, min(float(retry_after), RETRY_AFTER_CAP))
    return base_delay * (8 if long_backoff else 2) ** attempt + random.uniform(0, 1)


def request_with_retry(
    client: "Client",
    method: str,
    path: str,
    payload: dict | None,
    *,
    retries: int,
    base_delay: float,
    on_retry: "Callable[[int, int, float, bool], None] | None" = None,
) -> dict:
    """One request, retried while the failure is transient.

    Non-retryable failures (billing, auth, bad parameters, moderation
    declines) are raised to the caller on the first attempt.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return client.request_json(method, path, payload)
        except Exception as exc:  # noqa: BLE001 - re-raised below
            if classify_error(exc) != "retryable" or attempt >= retries:
                raise
            last_exc = exc
            long_backoff = is_high_demand(exc)
            sleep = backoff(base_delay, attempt, long_backoff=long_backoff, exc=exc)
            if on_retry:
                on_retry(attempt + 1, retries, sleep, long_backoff)
            time.sleep(sleep)
    raise last_exc  # pragma: no cover - the loop always returns or raises


class Client:
    """Minimal JSON client for the Spicy API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    def request_json(self, method: str, path: str, payload: dict | None = None) -> dict:
        """One authenticated JSON request. Raises ``ProviderError``."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url, data=data, headers=self._headers, method=method.upper()
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from None
        except urllib.error.URLError as exc:
            raise ProviderError(0, f"Connection to {self.base_url} failed: {exc.reason}") from None
        if not body.strip():
            return {}
        try:
            decoded = json.loads(body)
        except ValueError:
            raise ProviderError(200, f"Unreadable response body: {body[:_MAX_ERROR_CHARS]}") from None
        return decoded if isinstance(decoded, dict) else {"data": decoded}

    def download(self, url: str) -> bytes:
        """Fetch a generated artifact (CDN URL) into memory."""
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise ProviderError(exc.code, f"Download failed ({exc.code}) for {url}") from None
        except urllib.error.URLError as exc:
            raise ProviderError(0, f"Download failed for {url}: {exc.reason}") from None

    def _http_error(self, exc: urllib.error.HTTPError) -> ProviderError:
        raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
        try:
            payload = json.loads(raw)
            payload = payload if isinstance(payload, dict) else {}
        except ValueError:
            payload = {}
        message = error_message(payload) or raw.strip()[:_MAX_ERROR_CHARS] or exc.reason
        retry_after = None
        header = exc.headers.get("Retry-After") if exc.headers else None
        if header:
            try:
                retry_after = float(header)
            except (TypeError, ValueError):
                retry_after = None
        return ProviderError(exc.code, str(message), payload, retry_after)
