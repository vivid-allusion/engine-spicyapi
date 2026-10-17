"""Engine implementation for SpicyAPI (spicyapi.ai).

Gold interface: ``Engine(profile, output_dir, api_key, on_progress)`` with
``Engine.run(inputs) -> list[OutputFile]``. All SpicyAPI-specific logic lives
here and in the sibling modules (:mod:`transport`, :mod:`catalog`,
:mod:`payloads`, :mod:`tasks`); Generators stay provider-agnostic.

Wire shape, per the provider's own OpenAPI document
(https://docs.spicyapi.ai/openapi.yaml):

* every generation is asynchronous — ``POST /jobs/createTask`` with
  ``{"model", "input"}`` returns a task in ``queued`` state, then
  ``GET /jobs/recordInfo?taskId=…`` is polled until the state leaves
  ``{queued, running}`` and ``output.assets[]`` carries short-lived signed URLs;
* images and videos differ only in how long that takes, not in the shape of
  the call, so one code path serves both;
* an ``Idempotency-Key`` makes a retried submission return the original task
  instead of holding funds twice, which is what lets the batch loop retry
  safely.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import outputs, tasks, transport
from .catalog import Catalog, EndpointSpec, load_catalog
from .datatypes import EngineError, InputFile, OutputFile, ProgressEvent
from .payloads import build_input, compose_prompt

DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_TASK_TIMEOUT = 3600.0

CREATE_TASK_PATH = "jobs/createTask"


class Engine:
    PLATFORM: str = "spicyapi"
    PROVIDER_NAME: str = "SpicyAPI"
    PROVIDER_HOMEPAGE: str = "https://spicyapi.ai"
    API_KEY_ENV_VAR: str = "SPICY_API_KEY"
    API_KEY_PATTERN: str = r"^sk-spicy-"
    BASE_URL: str = transport.DEFAULT_BASE_URL

    def __init__(
        self,
        profile: dict,
        output_dir: str | Path,
        api_key: str | None = None,
        on_progress: Callable[[str], None] | None = None,
    ):
        self._profile = profile
        self._output_dir = Path(output_dir)
        self._api_key = api_key
        self._on_progress = on_progress
        self._prefix = profile.get("prompt_prefix", "")
        self._suffix = profile.get("prompt_suffix", "")
        self._base_url = str(profile.get("base_url") or profile.get("api_base_url") or self.BASE_URL)
        self._poll_interval = _non_negative_float(profile.get("poll_interval"), DEFAULT_POLL_INTERVAL)
        self._task_timeout = _positive_float(profile.get("timeout_seconds"), DEFAULT_TASK_TIMEOUT)
        self._catalog: Catalog = load_catalog()

    def run(self, inputs: list[InputFile]) -> list[OutputFile]:
        self._validate_preflight()

        endpoint = self._profile["endpoint"]
        spec = self._catalog.get(endpoint)
        params = dict(self._profile.get("parameters", {}))
        media_type = self._profile.get("media_type", "") or "image"
        self._output_dir.mkdir(parents=True, exist_ok=True)

        client = self._make_client()
        results: list[OutputFile] = []
        total = len(inputs)
        consecutive_retryable = 0
        circuit_max = int(self._profile.get("circuit_breaker_max", 3))

        for idx, item in enumerate(inputs):
            stem = item.path.stem
            current = idx + 1
            prefix = f"[{current}/{total}]"
            rel_dir = str(item.metadata.get("relative_dir", "") or "")
            dest_dir = self._output_dir / rel_dir if rel_dir else self._output_dir
            ts = datetime.now().strftime("%y%m%d_%H%M%S")
            ext = self._default_extension(media_type)
            expected = dest_dir / f"{ts}-{stem}-{idx}{ext}"

            delay = float(self._profile.get("delay_between_requests", 0.0))
            if idx > 0 and delay > 0:
                time.sleep(delay)

            prompt = compose_prompt(self._profile, item)
            if spec.prompt_required and not prompt:
                self._emit(f"{prefix} Empty prompt after applying prefix/suffix", level="error")
                results.append(
                    OutputFile(
                        source_path=item.path,
                        status="error",
                        error_msg="Empty prompt after applying prefix/suffix",
                        media_type=media_type,
                        expected_path=expected,
                    )
                )
                continue

            self._emit(f"{prefix} 📡 Calling SpicyAPI ({endpoint})...")
            self._emit(f"{prefix} 📝 Markdown: {item.path.name}")

            api_payload: dict | None = None
            try:
                api_input, warnings = build_input(self._profile, spec, params, prompt, item)
                for warning in warnings:
                    self._emit(f"{prefix} ⚠️  {warning}", level="warning")
                api_payload = {"model": spec.model, "input": api_input}
                record = self._dispatch(client, spec, api_payload, prefix)
                urls = tasks.asset_urls(record)
                self._emit(f"{prefix} 📦 Payload: {api_payload}")
                saved = self._save_results(
                    client, urls, media_type, stem, idx, prefix, current, total, rel_dir
                )
                if not saved:
                    results.append(
                        OutputFile(
                            source_path=item.path,
                            status="error",
                            error_msg="No output returned from SpicyAPI",
                            media_type=media_type,
                            expected_path=expected,
                        )
                    )
                    continue
                consecutive_retryable = 0
                for saved_path in saved:
                    results.append(
                        OutputFile(
                            source_path=item.path,
                            path=saved_path,
                            status="ok",
                            media_type=media_type,
                            metadata={"api_payload": api_payload},
                        )
                    )
                continue

            except EngineError as exc:
                self._emit(
                    f"{prefix} Error: {exc}",
                    level="error",
                    current=current,
                    total=total,
                    api_payload=api_payload,
                )
                results.append(
                    OutputFile(
                        source_path=item.path,
                        status="error",
                        error_msg=str(exc),
                        media_type=media_type,
                        expected_path=expected,
                    )
                )
                continue

            except Exception as exc:
                self._emit(
                    f"{prefix} Error: {exc}",
                    level="error",
                    current=current,
                    total=total,
                    api_payload=api_payload,
                )
                if transport.is_content_rejected(exc):
                    self._emit(
                        f"{prefix} 🚫 Declined by the model's own policy — nothing was charged.",
                        level="error",
                    )
                output = OutputFile(
                    source_path=item.path,
                    status="error",
                    error_msg=str(exc),
                    media_type=media_type,
                    expected_path=expected,
                )
                if transport.is_batch_fatal(exc):
                    self._emit(
                        f"{prefix} 🛑 Fatal provider error — aborting batch. "
                        f"Check the key, balance or spend cap. ({exc})",
                        level="error",
                    )
                    results.append(output)
                    break
                if transport.is_retryable(exc):
                    consecutive_retryable += 1
                    if consecutive_retryable >= circuit_max:
                        self._emit(
                            f"{prefix} 🛑 Circuit breaker tripped after "
                            f"{consecutive_retryable} consecutive retryable "
                            f"failures — aborting batch.",
                            level="error",
                        )
                        results.append(output)
                        break
                else:
                    consecutive_retryable = 0

            results.append(output)

        return results

    # ── provider calls ────────────────────────────────────────────────────

    def _dispatch(
        self, client: transport.Client, spec: EndpointSpec, payload: dict, prefix: str
    ) -> dict:
        """Create the task, wait for it, and return its terminal record."""
        request = self._make_request(client, prefix)
        task = tasks.create_task(
            request,
            payload,
            headers={"Idempotency-Key": uuid.uuid4().hex},
            emit=self._emit,
            prefix=prefix,
        )
        return tasks.await_task(
            request,
            task,
            poll_interval=self._poll_interval,
            timeout=self._task_timeout,
            emit=self._emit,
            prefix=prefix,
        )

    def _make_request(self, client: transport.Client, prefix: str) -> Callable[..., dict]:
        """A retrying request function bound to this batch's retry policy."""

        def note(attempt: int, total: int, sleep: float, long_backoff: bool) -> None:
            label = "model unavailable" if long_backoff else "rate limit"
            self._emit(f"{prefix} ⏳ {label}, retry {attempt}/{total} in {sleep:.1f}s")

        def request(
            method: str,
            path: str,
            payload: dict | None = None,
            *,
            headers: dict[str, str] | None = None,
            params: dict[str, str] | None = None,
        ) -> dict:
            return transport.request_with_retry(
                client,
                method,
                path,
                payload,
                headers=headers,
                params=params,
                retries=int(self._profile.get("retry_on_429", 4)),
                base_delay=float(self._profile.get("retry_base_delay", 2.0)),
                on_retry=note,
            )

        return request

    # ── outputs ───────────────────────────────────────────────────────────

    def _save_results(
        self,
        client: transport.Client,
        urls: list[str],
        media_type: str,
        stem: str,
        idx: int,
        prefix: str,
        current: int,
        total: int,
        rel_dir: str,
    ) -> list[Path]:
        dest_dir = self._output_dir / rel_dir if rel_dir else self._output_dir

        def on_saved(dest: Path) -> None:
            self._emit(
                f"{prefix} 💾 Saved: {dest.name}",
                current=current,
                total=total,
                saved_path=dest,
            )

        return outputs.save_results(
            client,
            dest_dir,
            urls,
            media_type,
            stem,
            idx,
            on_download=lambda: self._emit(f"{prefix} ⬇️  Downloading..."),
            on_saved=on_saved,
        )

    def _default_extension(self, media_type: str) -> str:
        """Profile-driven extension for precomputed (error-placeholder) names."""
        return outputs.default_extension(self._profile, media_type)

    # ── preflight ─────────────────────────────────────────────────────────

    def _validate_preflight(self) -> None:
        if not self._profile.get("endpoint"):
            raise EngineError("Missing or empty 'endpoint' in profile")
        if not self._resolve_api_key():
            raise EngineError(f"{self.API_KEY_ENV_VAR} not set in environment or .env file")

    def _resolve_api_key(self) -> str:
        if self._api_key:
            return self._api_key
        return os.environ.get(self.API_KEY_ENV_VAR, "")

    def _make_client(self) -> transport.Client:
        timeout = _positive_float(self._profile.get("http_timeout"), transport.DEFAULT_TIMEOUT)
        return transport.Client(
            api_key=self._resolve_api_key(), base_url=self._base_url, timeout=timeout
        )

    # ── progress ──────────────────────────────────────────────────────────

    def _emit(
        self,
        message: str,
        level: str = "info",
        current: int = 0,
        total: int = 0,
        saved_path: Path | None = None,
        api_payload: dict | None = None,
    ) -> None:
        if self._on_progress:
            self._on_progress(
                ProgressEvent(
                    message=message,
                    level=level,
                    current=current,
                    total=total,
                    saved_path=saved_path,
                    api_payload=api_payload,
                )
            )


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_negative_float(value: Any, default: float) -> float:
    """Like :func:`_positive_float`, but 0 is a real value (poll immediately)."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default
