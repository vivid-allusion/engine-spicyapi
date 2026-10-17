"""Video task polling.

The Spicy API is asynchronous for video: ``POST /v1/videos/generations`` (or
``/v1/videos/edits``) returns a task and the clip appears at
``GET /v1/videos/tasks/{id}`` once ``status`` reaches ``succeeded``. Split
from ``engine.py`` so the batch loop stays about control flow; the caller
passes its retrying request function, its emit hook and its timing.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .transport import ProviderError

RequestFn = Callable[[str, str, "dict | None"], dict]
EmitFn = Callable[[str], None]


def await_task(
    request: RequestFn,
    task: dict,
    *,
    poll_interval: float,
    timeout: float,
    emit: EmitFn,
    prefix: str = "",
) -> list[str]:
    """Poll a video task until it finishes; return the artifact URLs."""
    task_id = str(task.get("id") or "")
    if not task_id:
        raise ProviderError(200, "No task id in SpicyAPI response")
    poll_path = str(task.get("poll_url") or f"v1/videos/tasks/{task_id}")
    emit(
        f"{prefix} ⏳ Video task {task_id} {task.get('status') or 'queued'}"
        f" (cost {task.get('cost_usd')} USD)"
    )

    done = finished_urls(task, emit, prefix)
    if done:
        return done

    started = time.monotonic()
    last_status = str(task.get("status") or "")
    while True:
        if timeout and time.monotonic() - started > timeout:
            raise ProviderError(0, f"Video task {task_id} timed out after {timeout:.0f}s")
        time.sleep(poll_interval)
        current = request("GET", poll_path, None)
        status = str(current.get("status") or "")
        if status != last_status:
            last_status = status
            emit(f"{prefix} ⏳ Video task {status}")
        done = finished_urls(current, emit, prefix)
        if done:
            return done
        if status == "failed":
            reason = current.get("error") or current.get("error_code") or "unknown error"
            raise ProviderError(200, f"Video task failed: {reason}")


def finished_urls(task: dict, emit: EmitFn = lambda _msg: None, prefix: str = "") -> list[str]:
    """The clip URL when the task is done, else an empty list."""
    if str(task.get("status") or "") != "succeeded":
        return []
    output = task.get("output") if isinstance(task.get("output"), dict) else {}
    url = output.get("video_url")
    if not url:
        raise ProviderError(200, f"Video task {task.get('id')} succeeded without a video_url")
    emit(f"{prefix} ✅ Video task succeeded (cost {task.get('cost_usd')} USD)")
    return [str(url)]
