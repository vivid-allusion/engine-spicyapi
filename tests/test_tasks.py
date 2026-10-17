"""The asynchronous task surface: create, poll, collect."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine_spicyapi import tasks  # noqa: E402
from engine_spicyapi.transport import ProviderError  # noqa: E402


class FakeRequest:
    """Records every call and replays a scripted sequence of responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, path, payload=None, *, headers=None, params=None):
        self.calls.append({"method": method, "path": path, "payload": payload, "headers": headers, "params": params})
        if not self.responses:
            raise AssertionError("unexpected extra request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def envelope(data):
    return {"code": 200, "msg": "success", "request_id": "req_1", "data": data}


def test_create_task_returns_the_acceptance_record():
    request = FakeRequest(envelope({"taskId": "job_1", "state": "queued", "estimatedCost": "0.36"}))
    seen = []
    record = tasks.create_task(
        request,
        {"model": "a/b", "input": {"prompt": "hi"}},
        headers={"Idempotency-Key": "k1"},
        emit=seen.append,
    )
    assert record["taskId"] == "job_1"
    assert request.calls[0]["method"] == "POST"
    assert request.calls[0]["path"] == "jobs/createTask"
    assert request.calls[0]["headers"] == {"Idempotency-Key": "k1"}
    assert any("job_1 accepted" in message for message in seen)


def test_create_task_without_an_id_is_an_error():
    request = FakeRequest(envelope({"state": "queued"}))
    with pytest.raises(ProviderError) as excinfo:
        tasks.create_task(request, {})
    assert "no task id" in str(excinfo.value)


def test_await_task_polls_until_succeeded():
    request = FakeRequest(
        envelope({"taskId": "job_1", "state": "running"}),
        envelope(
            {
                "taskId": "job_1",
                "state": "succeeded",
                "cost": "0.36",
                "output": {"assets": [{"url": "https://cdn/out.png", "mime": "image/png"}]},
            }
        ),
    )
    record = tasks.await_task(request, {"taskId": "job_1", "state": "queued"}, poll_interval=0, timeout=10)
    assert record["state"] == "succeeded"
    assert [call["path"] for call in request.calls] == ["jobs/recordInfo", "jobs/recordInfo"]
    assert request.calls[0]["params"] == {"taskId": "job_1"}
    assert request.calls[0]["method"] == "GET"


def test_await_task_keeps_polling_while_assets_are_pending():
    request = FakeRequest(
        envelope(
            {
                "taskId": "job_1",
                "state": "succeeded",
                "output": {"assets": [{"pending": True, "mime": "video/mp4"}]},
            }
        ),
        envelope(
            {
                "taskId": "job_1",
                "state": "succeeded",
                "output": {"assets": [{"url": "https://cdn/out.mp4"}]},
            }
        ),
    )
    record = tasks.await_task(request, {"taskId": "job_1"}, poll_interval=0, timeout=10)
    assert tasks.asset_urls(record) == ["https://cdn/out.mp4"]
    assert len(request.calls) == 2


def test_await_task_raises_the_task_failure():
    request = FakeRequest(
        envelope(
            {
                "taskId": "job_1",
                "state": "failed",
                "errorCode": "content_rejected",
                "errorMessage": "the model declined it",
            }
        )
    )
    with pytest.raises(ProviderError) as excinfo:
        tasks.await_task(request, {"taskId": "job_1"}, poll_interval=0, timeout=10)
    message = str(excinfo.value)
    assert "content_rejected" in message
    assert "the model declined it" in message
    assert "nothing was charged" in message


def test_await_task_times_out_without_cancelling_the_task():
    request = FakeRequest(*[envelope({"taskId": "job_1", "state": "running"}) for _ in range(50)])
    with pytest.raises(ProviderError) as excinfo:
        tasks.await_task(request, {"taskId": "job_1"}, poll_interval=0, timeout=0.0001)
    assert "timed out" in str(excinfo.value)
    assert "keeps running" in str(excinfo.value)


def test_await_task_needs_a_task_id():
    with pytest.raises(ProviderError):
        tasks.await_task(FakeRequest(), {}, poll_interval=0, timeout=1)


def test_asset_urls_skips_pending_and_unavailable():
    record = {
        "output": {
            "assets": [
                {"url": "https://cdn/a.png"},
                {"pending": True},
                {"url": "https://cdn/gone.png", "unavailable": True},
                {"mime": "image/png"},
            ]
        }
    }
    assert tasks.asset_urls(record) == ["https://cdn/a.png"]


def test_asset_urls_tolerates_a_missing_output():
    assert tasks.asset_urls({}) == []
    assert tasks.asset_urls({"output": None}) == []
    assert tasks.asset_urls({"output": {"assets": "nope"}}) == []


def test_is_terminal():
    assert tasks.is_terminal("queued") is False
    assert tasks.is_terminal("running") is False
    assert tasks.is_terminal("succeeded") is True
    assert tasks.is_terminal("canceled") is True
    assert tasks.is_terminal(None) is True


def test_record_info_missing_data_is_an_error():
    request = FakeRequest({"code": 200, "msg": "success"})
    with pytest.raises(ProviderError):
        tasks.await_task(request, {"taskId": "job_1"}, poll_interval=0, timeout=1)
