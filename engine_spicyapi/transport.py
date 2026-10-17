"""HTTP transport for SpicyAPI (spicyapi.ai) — stdlib only.

One JSON client and one downloader. Every wire call the Engine makes goes
through here, so retry hints (``Retry-After``), the provider's error envelope
and error classification live in exactly one place.

SpicyAPI is plain REST, so the Engine carries no third-party SDK dependency:
``urllib.request`` is enough for JSON in / JSON out and for pulling result
files down. Result URLs are short-lived signed grants — they are fetched
**without** the API key, exactly as the provider requires.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlencode

DEFAULT_BASE_URL = "https://api.spicyapi.ai/api/v1"
DEFAULT_TIMEOUT = 600.0
DOWNLOAD_TIMEOUT = 300.0
RETRY_AFTER_CAP = 300.0
USER_AGENT = "engine-spicyapi/0.1 (+https://github.com/vivid-allusion/engine-spicyapi)"

_MAX_ERROR_CHARS = 600

#: Business codes that mean "every remaining Markdown file will fail the same
#: way": the key is bad, the balance is short, a spend cap is reached, or the
#: key/account/region is not allowed to run models at all.
FATAL_CODES = frozenset(
    {
        401,
        40201,
        40202,
        403,
        40301,
        40302,
        40303,
        40310,
    }
)

#: Business codes where waiting and trying again is the documented advice.
#: 50302 is deliberately absent: the provider says a retry of that failure must
#: use a *new* ``Idempotency-Key``, so the transport cannot safely retry it —
#: it surfaces per file and the user re-runs.
RETRYABLE_CODES = frozenset({429, 500, 503, 50301})

#: Codes that mean the request itself was wrong. Per file, never the batch.
_REQUEST_CODES = frozenset({400, 40003, 40004, 404, 409, 40901, 413})


class ProviderError(RuntimeError):
    """A failed SpicyAPI call.

    Attributes:
        status: HTTP status code, or 0 when the request never completed
            (connection error, timeout, bad JSON).
        payload: the decoded error envelope when there was one.
        retry_after: the ``Retry-After`` header in seconds, when sent.
        code: the provider's business code (``payload["code"]``) when present.
        request_id: the provider's correlation id, for support requests.
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
    def code(self) -> int | None:
        """Provider business code, when the body carries one."""
        code = self.payload.get("code")
        try:
            return int(code)
        except (TypeError, ValueError):
            return None

    @property
    def request_id(self) -> str:
        return str(self.payload.get("request_id") or "")


def error_message(payload: dict) -> str:
    """Human message out of SpicyAPI's envelope.

    Every JSON response uses ``{code, msg, data, request_id}``; the message is
    ``msg``. The OpenAI/Anthropic/Gemini compatibility layers are the documented
    exception and carry their own shapes, so those are read too.
    """
    if payload.get("msg"):
        return str(payload["msg"])
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("message", "detail", "code"):
            if error.get(key):
                return str(error[key])
    if isinstance(error, str) and error:
        return error
    if payload.get("message"):
        return str(payload["message"])
    return ""


def classify_error(exc: Exception) -> str:
    """Classify a provider exception.

    Returns ``"retryable"`` (rate limit, capacity — retrying helps),
    ``"fatal"`` (auth, balance, spend cap, region — the whole batch will fail)
    or ``"other"`` (bad parameters, a model that cannot serve the request, a
    declined generation — per file only, the next Markdown file may well
    succeed).
    """
    code = getattr(exc, "code", None)
    if code in FATAL_CODES:
        return "fatal"
    if code in RETRYABLE_CODES:
        return "retryable"
    if code in _REQUEST_CODES:
        return "other"
    status = getattr(exc, "status", None)
    if status in FATAL_CODES:
        return "fatal"
    if status in RETRYABLE_CODES:
        return "retryable"
    if status and 500 <= status < 600:
        return "retryable"
    return "other"


def is_batch_fatal(exc: Exception) -> bool:
    """True when every remaining Markdown file would fail the same way."""
    return classify_error(exc) == "fatal"


def is_retryable(exc: Exception) -> bool:
    return classify_error(exc) == "retryable"


def is_content_rejected(exc: Exception) -> bool:
    """A task the model's own policy declined (``errorCode`` content_rejected)."""
    return "content_rejected" in str(exc).lower()


def backoff(
    base_delay: float, attempt: int, *, long_backoff: bool = False, exc: Exception | None = None
) -> float:
    """Seconds to wait before retry ``attempt`` (0-based).

    The provider's ``Retry-After`` wins when it sends one; otherwise this is
    exponential, stretched 8x for capacity outages, which resolve far slower
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
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    on_retry: "Callable[[int, int, float, bool], None] | None" = None,
) -> dict:
    """One request, retried while the failure is transient.

    Non-retryable failures (auth, balance, bad parameters, declined
    generations) are raised to the caller on the first attempt.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return client.request_json(method, path, payload, headers=headers, params=params)
        except Exception as exc:  # noqa: BLE001 - re-raised below
            if not is_retryable(exc) or attempt >= retries:
                raise
            last_exc = exc
            long_backoff = getattr(exc, "status", None) == 503 or getattr(exc, "code", None) == 503
            sleep = backoff(base_delay, attempt, long_backoff=long_backoff, exc=exc)
            if on_retry:
                on_retry(attempt + 1, retries, sleep, long_backoff)
            time.sleep(sleep)
    raise last_exc  # pragma: no cover - the loop always returns or raises


class Client:
    """Minimal JSON client for SpicyAPI."""

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

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict:
        """One authenticated JSON request. Raises ``ProviderError``."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urlencode(params)}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            headers={**self._headers, **(headers or {})},
            method=method.upper(),
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
        """Fetch a result file (signed URL) into memory.

        Never sends the API key: result URLs are bearer access grants of their
        own and the provider rejects a request that attaches one.
        """
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
        request_id = str(payload.get("request_id") or "")
        if request_id:
            message = f"{message} (request_id {request_id})"
        retry_after = None
        header = exc.headers.get("Retry-After") if exc.headers else None
        if header:
            try:
                retry_after = float(header)
            except (TypeError, ValueError):
                retry_after = None
        return ProviderError(exc.code, str(message), payload, retry_after)
