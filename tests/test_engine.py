"""Engine orchestration: request flow, retries, error handling, saving.

Every provider call is faked — no network, no API key, no spend. The fake
client records the exact (method, path, payload) trip so the wire contract
HTTP paths and body shapes are asserted rather than assumed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine_spicyapi import Engine, EngineError, InputFile  # noqa: E402
from engine_spicyapi import engine as engine_module  # noqa: E402
from engine_spicyapi.catalog import load_catalog  # noqa: E402
from engine_spicyapi.transport import ProviderError, classify_error  # noqa: E402
from engine_spicyapi import transport  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"
JPEG = b"\xff\xd8\xff\xe0" + b"pixels"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"pixels"

IMG_TOML = """
id = "spicy-image-1"
label = "Spicy Image 1"
category = "IMG"
platform = "spicyapi"
endpoint = "spicy-image-1"
description = "Test image model."

[pricing]
base_cost = 0.06
currency = "USD"

[general]
kind = "image"
prompt = "required"
image_url_param = "image_url"

[params.size]
label = "Size"
type = "select"
default = "1024*1024"
options = [{ value = "1024*1024", label = "1024*1024 · Square" }]

[params.n]
label = "Images per Call"
type = "integer"
default = 1
min = 1
max = 6

[params.enhance_prompt]
label = "Enhance"
type = "boolean"
default = false
"""

VID_TOML = """
id = "spicy-motion-3"
label = "Spicy Motion 3"
category = "VID"
platform = "spicyapi"
endpoint = "spicy-motion-3"
description = "Test video model."

[pricing]
cost_per_second = 0.20
currency = "USD"

[general]
kind = "video"
prompt = "optional"
image_url_param = "image_url"
duration_type = "seconds"
duration_param_name = "duration"
slots = ["last_frame_url", "reference_image_urls"]

[params.resolution]
label = "Resolution"
type = "select"
default = "720P"
options = [{ value = "720P", label = "720P" }, { value = "1080P", label = "1080P" }]

[params.duration]
label = "Duration"
type = "integer"
default = 5
min = -1
max = 30
"""


class FakeClient:
    """Stands in for :class:`engine_spicyapi.transport.Client`."""

    def __init__(self, responses=(), downloads=None):
        self.responses = list(responses)
        self.downloads = downloads or {}
        self.calls: list[tuple[str, str, dict | None]] = []

    def request_json(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if not self.responses:
            raise AssertionError("FakeClient ran out of canned responses")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def download(self, url):
        return self.downloads.get(url, PNG)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Never really wait: the Engine's delays are asserted, not slept through."""
    sleeps: list[float] = []
    monkeypatch.setattr(engine_module.transport.time, "sleep", lambda seconds: sleeps.append(seconds))
    return sleeps


@pytest.fixture
def catalog_dir(tmp_path):
    catalog = tmp_path / "catalog"
    (catalog / "IMG-Models").mkdir(parents=True)
    (catalog / "VID-Models").mkdir(parents=True)
    (catalog / "IMG-Models" / "spicy-image-1.toml").write_text(IMG_TOML)
    (catalog / "VID-Models" / "spicy-motion-3.toml").write_text(VID_TOML)
    return catalog


def make_engine(tmp_path, catalog_dir, profile, client, on_progress=None):
    engine = Engine(
        profile=profile,
        output_dir=tmp_path / "out",
        api_key="sk-spicy-test",
        on_progress=on_progress,
    )
    engine._catalog = load_catalog(catalog_dir)
    engine._make_client = lambda: client
    return engine


def image_profile(**overrides):
    profile = {
        "platform": "spicyapi",
        "media_type": "image",
        "endpoint": "spicy-image-1",
        "parameters": {"size": "1024*1024", "n": 1},
    }
    profile.update(overrides)
    return profile


def video_profile(**overrides):
    profile = {
        "platform": "spicyapi",
        "media_type": "video",
        "endpoint": "spicy-motion-3",
        "duration_param_name": "duration",
        "slots": ["last_frame_url", "reference_image_urls"],
        "parameters": {"resolution": "720P", "duration": 5},
    }
    profile.update(overrides)
    return profile


def markdown(name="shot-001.md", prompt="a woman in a red dress", **overrides):
    base = {"path": Path(name), "prompt": prompt}
    base.update(overrides)
    return InputFile(**base)


# ── preflight ─────────────────────────────────────────────────────────────


def test_constructor_is_pure(tmp_path, catalog_dir, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("the constructor must not build a transport")

    monkeypatch.setattr(engine_module.transport, "Client", explode)
    Engine(profile=image_profile(), output_dir=tmp_path, api_key="sk-spicy-test")


def test_missing_endpoint_raises(tmp_path, catalog_dir):
    engine = make_engine(tmp_path, catalog_dir, image_profile(endpoint=""), FakeClient())
    with pytest.raises(EngineError):
        engine.run([markdown()])


def test_missing_api_key_raises(tmp_path, catalog_dir, monkeypatch):
    monkeypatch.delenv("SPICYAPI_API_KEY", raising=False)
    engine = Engine(profile=image_profile(), output_dir=tmp_path)
    engine._catalog = load_catalog(catalog_dir)
    with pytest.raises(EngineError, match="SPICYAPI_API_KEY"):
        engine.run([markdown()])


# ── image flow ────────────────────────────────────────────────────────────


def test_image_run_posts_to_images_generations_and_saves(tmp_path, catalog_dir):
    client = FakeClient(
        responses=[{"object": "image.generation", "data": [{"url": "https://cdn.example/a.png"}]}],
        downloads={"https://cdn.example/a.png": PNG},
    )
    engine = make_engine(tmp_path, catalog_dir, image_profile(), client)

    results = engine.run([markdown()])

    assert client.calls[0][0] == "POST"
    assert client.calls[0][1] == "v1/images/generations"
    payload = client.calls[0][2]
    assert payload["model"] == "spicy-image-1"
    assert payload["prompt"] == "a woman in a red dress"
    assert payload["size"] == "1024*1024"

    assert len(results) == 1
    assert results[0].status == "ok"
    assert results[0].path is not None and results[0].path.exists()
    assert results[0].path.name.endswith("-shot-001-0.png")
    assert results[0].metadata["api_payload"]["model"] == "spicy-image-1"


def test_multiple_outputs_get_index_suffixes(tmp_path, catalog_dir):
    client = FakeClient(
        responses=[
            {
                "object": "image.generation",
                "data": [{"url": "https://cdn.example/a.png"}, {"url": "https://cdn.example/b.png"}],
            }
        ]
    )
    engine = make_engine(tmp_path, catalog_dir, image_profile(parameters={"n": 2}), client)

    results = engine.run([markdown()])

    assert [r.status for r in results] == ["ok", "ok"]
    assert results[0].path.name.endswith("-shot-001-0_0.png")
    assert results[1].path.name.endswith("-shot-001-0_1.png")


def test_relative_dir_is_mirrored_under_the_output_dir(tmp_path, catalog_dir):
    client = FakeClient(
        responses=[{"data": [{"url": "https://cdn.example/a.png"}]}]
    )
    engine = make_engine(tmp_path, catalog_dir, image_profile(), client)

    results = engine.run([markdown(metadata={"relative_dir": "series-a"})])

    assert results[0].path.parent.name == "series-a"


def test_sniffed_extension_wins_over_the_guess(tmp_path, catalog_dir):
    client = FakeClient(
        responses=[{"data": [{"url": "https://cdn.example/a"}]}],
        downloads={"https://cdn.example/a": JPEG},
    )
    engine = make_engine(tmp_path, catalog_dir, image_profile(), client)

    results = engine.run([markdown()])

    assert results[0].path.suffix == ".jpg"


def test_empty_prompt_fails_before_any_api_call(tmp_path, catalog_dir):
    client = FakeClient()
    engine = make_engine(tmp_path, catalog_dir, image_profile(), client)

    results = engine.run([markdown(prompt="")])

    assert client.calls == []
    assert results[0].status == "error"
    assert "Empty prompt" in results[0].error_msg
    assert results[0].expected_path.name.endswith("-shot-001-0.png")


def test_video_model_accepts_an_empty_prompt(tmp_path, catalog_dir):
    client = FakeClient(
        responses=[
            {"id": "vid_1", "status": "succeeded", "output": {"video_url": "https://cdn.example/v.mp4"}}
        ],
        downloads={"https://cdn.example/v.mp4": MP4},
    )
    engine = make_engine(tmp_path, catalog_dir, video_profile(), client)

    results = engine.run([markdown(prompt="", reference_urls=["https://cdn.example/first.png"])])

    assert results[0].status == "ok"
    assert "prompt" not in client.calls[0][2]


# ── video flow ────────────────────────────────────────────────────────────


def test_video_task_is_polled_until_succeeded(tmp_path, catalog_dir, no_sleep):
    client = FakeClient(
        responses=[
            {
                "id": "vid_1",
                "status": "queued",
                "cost_usd": 1.0,
                "poll_url": "/v1/videos/tasks/vid_1",
            },
            {"id": "vid_1", "status": "processing"},
            {
                "id": "vid_1",
                "status": "succeeded",
                "output": {"video_url": "https://cdn.example/v.mp4"},
                "cost_usd": 1.5,
            },
        ],
        downloads={"https://cdn.example/v.mp4": MP4},
    )
    engine = make_engine(tmp_path, catalog_dir, video_profile(), client)

    results = engine.run([markdown(metadata={"duration": 5})])

    assert client.calls[0][1] == "v1/videos/generations"
    assert client.calls[1][0] == "GET"
    assert "tasks/vid_1" in client.calls[1][1]
    assert client.calls[1][2] is None
    assert no_sleep == [10.0, 10.0]
    assert results[0].status == "ok"
    assert results[0].path.suffix == ".mp4"


def test_video_task_failure_is_reported(tmp_path, catalog_dir):
    client = FakeClient(
        responses=[
            {"id": "vid_2", "status": "queued", "poll_url": "/v1/videos/tasks/vid_2"},
            {
                "id": "vid_2",
                "status": "failed",
                "error": "model declined the content",
                "error_code": "blocked",
            },
        ]
    )
    engine = make_engine(tmp_path, catalog_dir, video_profile(), client)

    results = engine.run([markdown()])

    assert results[0].status == "error"
    assert "model declined the content" in results[0].error_msg
    assert results[0].expected_path.name.endswith(".mp4")


def test_succeeded_task_without_a_video_url_is_an_error(tmp_path, catalog_dir):
    client = FakeClient(responses=[{"id": "vid_3", "status": "succeeded", "output": {}}])
    engine = make_engine(tmp_path, catalog_dir, video_profile(), client)

    results = engine.run([markdown()])

    assert results[0].status == "error"
    assert "without a video_url" in results[0].error_msg


# ── error handling ────────────────────────────────────────────────────────


def test_parameter_error_keeps_the_batch_going(tmp_path, catalog_dir):
    client = FakeClient(
        responses=[
            ProviderError(400, "bad size", {"error": {"message": "bad size"}}),
            {"data": [{"url": "https://cdn.example/a.png"}]},
        ]
    )
    engine = make_engine(tmp_path, catalog_dir, image_profile(), client)

    results = engine.run([markdown("a.md"), markdown("b.md")])

    assert [r.status for r in results] == ["error", "ok"]
    assert results[0].error_msg == "bad size"
    assert results[0].expected_path.name.endswith("-a-0.png")
    assert len(client.calls) == 2


def test_insufficient_balance_aborts_the_whole_batch(tmp_path, catalog_dir):
    client = FakeClient(
        responses=[
            ProviderError(
                402,
                "Insufficient balance",
                {"error": {"message": "Insufficient balance", "type": "insufficient_balance"}},
            )
        ]
    )
    engine = make_engine(tmp_path, catalog_dir, image_profile(), client)

    results = engine.run([markdown("a.md"), markdown("b.md"), markdown("c.md")])

    assert len(results) == 1
    assert results[0].status == "error"
    assert len(client.calls) == 1
    assert classify_error(ProviderError(402, "Insufficient balance")) == "fatal"


def test_bad_api_key_is_fatal(tmp_path, catalog_dir):
    client = FakeClient(
        responses=[ProviderError(401, "Missing or invalid API key", {"error": {"message": "nope"}})]
    )
    engine = make_engine(tmp_path, catalog_dir, image_profile(), client)

    results = engine.run([markdown("a.md"), markdown("b.md")])

    assert len(results) == 1
    assert len(client.calls) == 1


def test_moderation_decline_is_per_file_and_names_the_appeal(tmp_path, catalog_dir):
    events = []
    client = FakeClient(
        responses=[
            ProviderError(
                422,
                "Prompt blocked",
                {
                    "error": {
                        "message": "Prompt blocked by moderation",
                        "type": "invalid_request_error",
                        "moderation_id": "mod_123",
                    }
                },
            ),
            {"data": [{"url": "https://cdn.example/a.png"}]},
        ]
    )
    engine = make_engine(tmp_path, catalog_dir, image_profile(), client, on_progress=events.append)

    results = engine.run([markdown("a.md"), markdown("b.md")])

    assert [r.status for r in results] == ["error", "ok"]
    assert len(client.calls) == 2
    messages = " ".join(event.message for event in events)
    assert "mod_123" in messages
    assert "nothing was charged" in messages


def test_rate_limit_is_retried_and_then_succeeds(tmp_path, catalog_dir, no_sleep):
    client = FakeClient(
        responses=[
            ProviderError(429, "rate limited", {"error": {"message": "slow down"}}, retry_after=3.0),
            {"data": [{"url": "https://cdn.example/a.png"}]},
        ]
    )
    engine = make_engine(tmp_path, catalog_dir, image_profile(), client)

    results = engine.run([markdown()])

    assert results[0].status == "ok"
    assert no_sleep == [3.0]
    assert len(client.calls) == 2


def test_capacity_503_uses_a_longer_backoff():
    capacity = ProviderError(503, "model at capacity", {"error": {"message": "capacity"}})
    rate_limit = ProviderError(429, "rate limited", {"error": {"message": "slow down"}})

    assert transport.backoff(2.0, 1, long_backoff=True, exc=capacity) >= 16.0
    assert transport.backoff(2.0, 1, long_backoff=False, exc=rate_limit) >= 4.0


def test_retry_after_wins_over_exponential_backoff():
    limited = ProviderError(429, "rate limited", {}, retry_after=42.0)
    assert transport.backoff(2.0, 3, exc=limited) == 42.0


def test_circuit_breaker_trips_after_consecutive_rate_limits(tmp_path, catalog_dir):
    responses = [
        ProviderError(429, "rate limited", {"error": {"message": "slow down"}})
        for _ in range(3)
    ]
    client = FakeClient(responses=responses)
    engine = make_engine(
        tmp_path, catalog_dir, image_profile(retry_on_429=0, circuit_breaker_max=2), client
    )

    results = engine.run([markdown("a.md"), markdown("b.md"), markdown("c.md")])

    assert len(results) == 2
    assert len(client.calls) == 2


def test_dry_run_response_is_not_saved(tmp_path, catalog_dir):
    client = FakeClient(
        responses=[{"object": "dry_run", "cost_usd": 0.06, "moderation": {"allowed": True}}]
    )
    engine = make_engine(
        tmp_path, catalog_dir, image_profile(parameters={"dry_run": True}), client
    )

    results = engine.run([markdown()])

    assert results[0].status == "error"
    assert "dry_run" in results[0].error_msg
    assert results[0].path is None


def test_too_many_urls_for_a_single_slot_is_a_per_file_error(tmp_path, catalog_dir):
    client = FakeClient()
    engine = make_engine(tmp_path, catalog_dir, video_profile(), client)

    results = engine.run(
        [
            markdown(
                references={"last_frame_url": ["u1", "u2"]},
            )
        ]
    )

    assert client.calls == []
    assert results[0].status == "error"
    assert "single media URL" in results[0].error_msg


# ── progress ──────────────────────────────────────────────────────────────


def test_progress_events_carry_saved_paths_and_payloads(tmp_path, catalog_dir):
    events = []
    client = FakeClient(responses=[{"data": [{"url": "https://cdn.example/a.png"}]}])
    engine = make_engine(tmp_path, catalog_dir, image_profile(), client)
    engine._on_progress = events.append

    engine.run([markdown()])

    saved = [event for event in events if event.saved_path]
    assert saved and saved[0].saved_path.exists()
    assert any("Calling SpicyAPI" in event.message for event in events)


def test_error_progress_event_carries_the_rejected_payload(tmp_path, catalog_dir):
    events = []
    client = FakeClient(responses=[ProviderError(400, "bad params", {"error": {"message": "bad"}})])
    engine = make_engine(tmp_path, catalog_dir, image_profile(), client)
    engine._on_progress = events.append

    engine.run([markdown()])

    errors = [event for event in events if event.level == "error" and event.api_payload]
    assert errors and errors[0].api_payload["model"] == "spicy-image-1"


def test_fps_warning_is_surfaced(tmp_path, catalog_dir):
    events = []
    client = FakeClient(responses=[{"data": [{"url": "https://cdn.example/a.png"}]}])
    engine = make_engine(
        tmp_path, catalog_dir, image_profile(parameters={"fps": 24}), client
    )
    engine._on_progress = events.append

    engine.run([markdown()])

    assert any("fps" in event.message and event.level == "warning" for event in events)
    assert "fps" not in client.calls[0][2]
