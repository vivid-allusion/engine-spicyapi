"""Engine orchestration: batch loop, error handling, saving, progress.

Every provider call is faked — no network, no API key, no spend. The fake
client records the exact trip so the wire contract (paths, bodies, headers) is
asserted rather than assumed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine_spicyapi import Engine, EngineError, InputFile  # noqa: E402
from engine_spicyapi import engine as engine_module  # noqa: E402
from engine_spicyapi.catalog import Catalog, EndpointSpec  # noqa: E402
from engine_spicyapi.transport import ProviderError  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"pixels"

IMG = EndpointSpec(
    id="seedream-5-0-pro-text-to-image",
    model="bytedance/seedream-5.0-pro/text-to-image",
    category="IMG",
    kind="image",
    label="Seedream 5.0 Pro",
    prompt_mode="required",
    params={"resolution": {"type": "select", "default": "2k"}, "num_outputs": {"type": "integer", "default": 1}},
)

I2V = EndpointSpec(
    id="wan-3-0-image-to-video",
    model="alibaba/wan-3.0/image-to-video",
    category="VID",
    kind="video",
    label="Wan 3.0",
    image_url_param="image_url",
    slots=("last_image_url",),
    prompt_mode="optional",
    params={"duration_seconds": {"type": "integer", "default": 5}},
)


def envelope(data):
    return {"code": 200, "msg": "success", "request_id": "req_1", "data": data}


def accepted(task_id="job_1"):
    return envelope({"taskId": task_id, "state": "queued", "estimatedCost": "0.036", "deadlineAt": "x"})


def succeeded(url, mime="image/png"):
    return envelope(
        {
            "taskId": "job_1",
            "state": "succeeded",
            "cost": "0.036",
            "settled": True,
            "output": {"assets": [{"url": url, "mime": mime}]},
        }
    )


class FakeClient:
    def __init__(self, script, payload=PNG):
        self.script = list(script)
        self.payload = payload
        self.calls = []
        self.downloads = []

    def request_json(self, method, path, payload=None, *, headers=None, params=None):
        self.calls.append(
            {"method": method, "path": path, "payload": payload, "headers": headers, "params": params}
        )
        if not self.script:
            raise AssertionError(f"unexpected extra request: {method} {path}")
        response = self.script.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def download(self, url):
        self.downloads.append(url)
        return self.payload


def make_engine(tmp_path, monkeypatch, client, spec, **profile):
    monkeypatch.setattr(engine_module, "load_catalog", lambda *a, **k: Catalog({spec.model: spec}))
    monkeypatch.setattr(engine_module.Engine, "_make_client", lambda self: client)
    base = {
        "platform": "spicyapi",
        "endpoint": spec.model,
        "media_type": "video" if spec.kind == "video" else "image",
        "parameters": {},
        "poll_interval": 0,
        "retry_on_429": 0,
    }
    base.update(profile)
    return Engine(base, tmp_path, api_key="sk-spicy-test")


def markdown(tmp_path, name="shot.md", prompt="a paper lantern", refs=None):
    path = tmp_path / name
    path.write_text(prompt + "\n")
    return InputFile(path=path, prompt=prompt, reference_urls=list(refs or []))


def test_happy_path_saves_the_result_and_records_the_payload(tmp_path, monkeypatch):
    client = FakeClient([accepted(), succeeded("https://cdn/out.png")])
    engine = make_engine(tmp_path, monkeypatch, client, IMG, parameters={"resolution": "1k"})
    events = []
    engine._on_progress = events.append

    results = engine.run([markdown(tmp_path)])

    assert len(results) == 1
    assert results[0].status == "ok"
    assert results[0].path.read_bytes() == PNG
    assert results[0].metadata["api_payload"] == {
        "model": "bytedance/seedream-5.0-pro/text-to-image",
        "input": {"resolution": "1k", "prompt": "a paper lantern"},
    }
    assert client.calls[0]["path"] == "jobs/createTask"
    assert client.calls[1]["path"] == "jobs/recordInfo"
    assert client.downloads == ["https://cdn/out.png"]
    assert any(event.message.startswith("[1/1] 📡") for event in events)


def test_every_task_carries_an_idempotency_key(tmp_path, monkeypatch):
    client = FakeClient([accepted(), succeeded("https://cdn/out.png")])
    engine = make_engine(tmp_path, monkeypatch, client, IMG)
    engine.run([markdown(tmp_path)])
    key = client.calls[0]["headers"]["Idempotency-Key"]
    assert key and len(key) == 32


def test_multiple_assets_are_all_saved(tmp_path, monkeypatch):
    response = envelope(
        {
            "taskId": "job_1",
            "state": "succeeded",
            "output": {
                "assets": [
                    {"url": "https://cdn/a.png"},
                    {"url": "https://cdn/b.png"},
                ]
            },
        }
    )
    client = FakeClient([accepted(), response])
    engine = make_engine(tmp_path, monkeypatch, client, IMG)
    results = engine.run([markdown(tmp_path)])
    assert len(results) == 2
    assert {r.path.name for r in results} == {
        r.path.name for r in results if r.path.name.endswith(("_0.png", "_1.png"))
    }


def test_video_profile_saves_an_mp4(tmp_path, monkeypatch):
    client = FakeClient([accepted(), succeeded("https://cdn/out.mp4", "video/mp4")], payload=MP4)
    engine = make_engine(tmp_path, monkeypatch, client, I2V, parameters={"duration_seconds": 5})
    results = engine.run([markdown(tmp_path, refs=["https://cdn/first.png"])])
    assert results[0].path.suffix == ".mp4"
    assert client.calls[0]["payload"]["input"]["image_url"] == "https://cdn/first.png"


def test_a_failed_task_becomes_an_error_result_with_an_expected_path(tmp_path, monkeypatch):
    failed = envelope(
        {
            "taskId": "job_1",
            "state": "failed",
            "errorCode": "content_rejected",
            "errorMessage": "declined",
        }
    )
    client = FakeClient([accepted(), failed])
    engine = make_engine(tmp_path, monkeypatch, client, IMG)
    results = engine.run([markdown(tmp_path)])
    assert results[0].status == "error"
    assert results[0].path is None
    assert results[0].expected_path is not None
    assert results[0].expected_path.suffix == ".png"
    assert "content_rejected" in results[0].error_msg


def test_a_fatal_error_aborts_the_batch(tmp_path, monkeypatch):
    fatal = ProviderError(401, "bad key", {"code": 401, "msg": "bad key", "request_id": "r"})
    client = FakeClient([fatal])
    engine = make_engine(tmp_path, monkeypatch, client, IMG)
    results = engine.run([markdown(tmp_path, "a.md"), markdown(tmp_path, "b.md")])
    assert len(results) == 1
    assert results[0].status == "error"
    assert len(client.calls) == 1


def test_a_retryable_error_is_retried_by_the_transport(tmp_path, monkeypatch):
    rate_limited = ProviderError(429, "slow down", {"code": 429, "msg": "slow down"})
    client = FakeClient([rate_limited, accepted(), succeeded("https://cdn/out.png")])
    engine = make_engine(tmp_path, monkeypatch, client, IMG, retry_on_429=1, retry_base_delay=0)
    results = engine.run([markdown(tmp_path)])
    assert results[0].status == "ok"
    assert [call["path"] for call in client.calls] == ["jobs/createTask", "jobs/createTask", "jobs/recordInfo"]


def test_the_circuit_breaker_trips_on_repeated_retryable_failures(tmp_path, monkeypatch):
    # request_with_retry is bypassed by the fake client, so a retryable error
    # surfaces once per Markdown file and the batch loop counts them.
    rate_limited = ProviderError(429, "slow down", {"code": 429, "msg": "slow down"})
    client = FakeClient([rate_limited, rate_limited, rate_limited, rate_limited])
    engine = make_engine(tmp_path, monkeypatch, client, IMG, circuit_breaker_max=2)
    results = engine.run([markdown(tmp_path, f"{i}.md") for i in range(4)])
    assert len(results) == 2
    assert all(result.status == "error" for result in results)


def test_a_text_only_model_refuses_reference_media_per_file(tmp_path, monkeypatch):
    client = FakeClient([accepted(), succeeded("https://cdn/out.png")])
    engine = make_engine(tmp_path, monkeypatch, client, IMG)
    results = engine.run(
        [
            markdown(tmp_path, "with-ref.md", refs=["https://cdn/ref.png"]),
            markdown(tmp_path, "clean.md"),
        ]
    )
    assert results[0].status == "error"
    assert "takes no media input" in results[0].error_msg
    assert results[1].status == "ok"
    assert len(client.calls) == 2  # only the clean file reached the API


def test_empty_prompt_on_a_required_model_fails_before_any_call(tmp_path, monkeypatch):
    client = FakeClient([])
    engine = make_engine(tmp_path, monkeypatch, client, IMG)
    results = engine.run([markdown(tmp_path, prompt="")])
    assert results[0].status == "error"
    assert "Empty prompt" in results[0].error_msg
    assert client.calls == []


def test_an_undeclared_profile_param_is_dropped_with_a_visible_warning(tmp_path, monkeypatch):
    client = FakeClient([accepted(), succeeded("https://cdn/out.png")])
    engine = make_engine(tmp_path, monkeypatch, client, IMG, parameters={"fps": 24})
    events = []
    engine._on_progress = events.append
    engine.run([markdown(tmp_path)])
    assert "fps" not in client.calls[0]["payload"]["input"]
    assert any("does not declare 'fps'" in event.message for event in events)


def test_a_success_without_assets_is_an_error(tmp_path, monkeypatch):
    empty = envelope({"taskId": "job_1", "state": "succeeded", "output": {"assets": []}})
    client = FakeClient([accepted(), empty])
    engine = make_engine(tmp_path, monkeypatch, client, IMG)
    results = engine.run([markdown(tmp_path)])
    assert results[0].status == "error"
    assert "No output returned" in results[0].error_msg


def test_missing_api_key_is_a_preflight_error(tmp_path, monkeypatch):
    monkeypatch.delenv("SPICY_API_KEY", raising=False)
    monkeypatch.setattr(engine_module, "load_catalog", lambda *a, **k: Catalog({IMG.model: IMG}))
    engine = Engine({"endpoint": IMG.model, "parameters": {}}, tmp_path)
    try:
        engine.run([markdown(tmp_path)])
    except EngineError as exc:
        assert "SPICY_API_KEY" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected EngineError")


def test_missing_endpoint_is_a_preflight_error(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_module, "load_catalog", lambda *a, **k: Catalog())
    engine = Engine({"parameters": {}}, tmp_path, api_key="sk-spicy-test")
    try:
        engine.run([markdown(tmp_path)])
    except EngineError as exc:
        assert "endpoint" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected EngineError")


def test_relative_dir_places_the_output(tmp_path, monkeypatch):
    client = FakeClient([accepted(), succeeded("https://cdn/out.png")])
    engine = make_engine(tmp_path, monkeypatch, client, IMG)
    item = markdown(tmp_path)
    item.metadata["relative_dir"] = "scenes"
    results = engine.run([item])
    assert results[0].path.parent == tmp_path / "scenes"


def test_list_standby_profiles_filters_by_media_type():
    from engine_spicyapi import list_standby_profiles

    image = list_standby_profiles("IMG")
    video = list_standby_profiles("VID")
    assert image and video
    assert all(path.parent.name == "IMG" for path in image)
    assert all(path.parent.name == "VID" for path in video)
    assert list_standby_profiles("nope") == []
