"""The asynchronous task surface: create, poll, collect.

SpicyAPI runs **every** generation as a task, images included::

    POST /jobs/createTask  -> {"taskId", "state": "queued", "estimatedCost", ...}
    GET  /jobs/recordInfo?taskId=...  -> the task record, including output.assets

Split from ``engine.py`` so the batch loop stays about control flow; the caller
passes its retrying request function, its emit hook and its timing.

Terminal is tested as *"the state is not ``queued`` or ``running``"* rather
than by enumerating terminal values, so historical ``canceled`` records and any
future terminal state cannot cause an endless poll.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .transport import ProviderError

RequestFn = Callable[..., dict]
EmitFn = Callable[[str], None]

#: States a task can still leave on its own.
LIVE_STATES = frozenset({"queued", "running"})


def is_terminal(state: object) -> bool:
    return str(state or "") not in LIVE_STATES


def create_task(
    request: RequestFn,
    body: dict,
    *,
    headers: dict[str, str] | None = None,
    emit: EmitFn = lambda _msg: None,
    prefix: str = "",
) -> dict:
    """Submit one task and return the acceptance record.

    The caller supplies ``Idempotency-Key`` in ``headers``: with it, a retried
    submission returns the original task instead of charging a second time.
    """
    envelope = request("POST", "jobs/createTask", body, headers=headers)
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict) or not data.get("taskId"):
        raise ProviderError(200, "createTask returned no task id")
    emit(
        f"{prefix} 🧾 Task {data['taskId']} accepted "
        f"({data.get('state') or 'queued'}, hold {data.get('estimatedCost')} USD)"
    )
    return data


def await_task(
    request: RequestFn,
    task: dict,
    *,
    poll_interval: float,
    timeout: float,
    emit: EmitFn = lambda _msg: None,
    prefix: str = "",
) -> dict:
    """Poll a task until it reaches a terminal state; return its record."""
    task_id = str(task.get("taskId") or "")
    if not task_id:
        raise ProviderError(200, "No task id in SpicyAPI response")

    started = time.monotonic()
    last_state = str(task.get("state") or "")
    while True:
        if timeout and time.monotonic() - started > timeout:
            raise ProviderError(
                0,
                f"Task {task_id} timed out after {timeout:.0f}s — it keeps running "
                f"on SpicyAPI; collect it later with recordInfo",
            )
        time.sleep(poll_interval)
        record = _record(request, task_id)
        state = str(record.get("state") or "")
        if state != last_state:
            last_state = state
            emit(f"{prefix} ⏳ Task {state}")
        if not is_terminal(state):
            continue
        if state != "succeeded":
            raise ProviderError(200, _failure_message(record))
        if _assets_pending(record):
            continue
        emit(f"{prefix} ✅ Task succeeded (cost {record.get('cost')} USD)")
        return record


def asset_urls(record: dict) -> list[str]:
    """Downloadable result URLs from a succeeded task record.

    Assets still being copied (``pending``) or gone for good (``unavailable``)
    carry no ``url``; both are skipped rather than guessed at.
    """
    output = record.get("output") if isinstance(record.get("output"), dict) else {}
    assets = output.get("assets")
    if not isinstance(assets, list):
        return []
    urls: list[str] = []
    for asset in assets:
        if isinstance(asset, dict) and asset.get("url") and not asset.get("unavailable"):
            urls.append(str(asset["url"]))
    return urls


def _assets_pending(record: dict) -> bool:
    output = record.get("output") if isinstance(record.get("output"), dict) else {}
    assets = output.get("assets")
    if not isinstance(assets, list):
        return False
    return any(isinstance(asset, dict) and asset.get("pending") for asset in assets)


def _record(request: RequestFn, task_id: str) -> dict:
    envelope = request("GET", "jobs/recordInfo", params={"taskId": task_id})
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict):
        raise ProviderError(200, f"recordInfo returned no record for task {task_id}")
    return data


def _failure_message(record: dict) -> str:
    code = str(record.get("errorCode") or "failed")
    message = str(record.get("errorMessage") or "").strip()
    task_id = str(record.get("taskId") or "")
    text = f"Task {task_id} ended as {record.get('state')}: {code}"
    if message:
        text += f" — {message}"
    if code == "content_rejected":
        text += " (the model's own policy declined it; nothing was charged)"
    return text
