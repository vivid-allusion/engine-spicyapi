"""Saving and polling helpers — the two pieces split out of engine.py."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine_spicyapi import outputs, tasks  # noqa: E402
from engine_spicyapi.transport import ProviderError  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"
JPEG = b"\xff\xd8\xff\xe0" + b"pixels"
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 "
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"pixels"


@pytest.mark.parametrize(
    ("data", "expected"),
    [(PNG, ".png"), (JPEG, ".jpg"), (WEBP, ".webp"), (MP4, ".mp4")],
)
def test_sniff_extension_reads_the_magic_bytes(data, expected):
    assert outputs.sniff_extension(data, "image") == expected


def test_sniff_extension_falls_back_to_the_media_type():
    assert outputs.sniff_extension(b"unrecognised", "video") == ".mp4"
    assert outputs.sniff_extension(b"unrecognised", "image") == ".png"


def test_default_extension_honours_a_profile_output_format():
    profile = {"parameters": {"output_format": ".PNG"}}
    assert outputs.default_extension(profile, "image") == ".png"


def test_default_extension_ignores_a_nonsense_format():
    profile = {"parameters": {"output_format": "image/png"}}
    assert outputs.default_extension(profile, "image") == ".png"


def test_clean_format():
    assert outputs.clean_format(".jpg") == "jpg"
    assert outputs.clean_format("") == ""
    assert outputs.clean_format("a/b") == ""


class RecordingClient:
    def __init__(self, payload=PNG):
        self.payload = payload
        self.urls: list[str] = []

    def download(self, url):
        self.urls.append(url)
        return self.payload


def test_save_results_names_single_and_multiple_outputs(tmp_path):
    client = RecordingClient()
    single = outputs.save_results(client, tmp_path, ["u1"], "image", "shot", 0)
    multiple = outputs.save_results(client, tmp_path, ["u1", "u2"], "image", "shot", 1)

    assert single[0].name.endswith("-shot-0.png")
    assert [p.name for p in multiple] == [
        p.name for p in multiple if p.name.endswith(("-shot-1_0.png", "-shot-1_1.png"))
    ]
    assert all(p.exists() for p in single + multiple)


def test_save_results_with_no_urls_writes_nothing(tmp_path):
    assert outputs.save_results(RecordingClient(), tmp_path, [], "image", "shot", 0) == []


def test_finished_urls_only_returns_a_url_on_success():
    assert tasks.finished_urls({"status": "processing"}) == []
    assert tasks.finished_urls(
        {"status": "succeeded", "output": {"video_url": "https://cdn.example/v.mp4"}}
    ) == ["https://cdn.example/v.mp4"]


def test_finished_urls_refuses_a_success_without_a_url():
    with pytest.raises(ProviderError, match="without a video_url"):
        tasks.finished_urls({"id": "vid_1", "status": "succeeded", "output": {}})


def test_await_task_returns_immediately_when_the_task_is_already_done():
    calls = []

    def request(method, path, payload):
        calls.append((method, path))
        return {}

    urls = tasks.await_task(
        request,
        {"id": "v", "status": "succeeded", "output": {"video_url": "https://cdn.example/v.mp4"}},
        poll_interval=0,
        timeout=1,
        emit=lambda _msg: None,
    )
    assert urls == ["https://cdn.example/v.mp4"]
    assert calls == []


def test_await_task_times_out(monkeypatch):
    monkeypatch.setattr(tasks.time, "sleep", lambda _seconds: None)
    clock = iter([0.0, 5.0, 500.0])
    monkeypatch.setattr(tasks.time, "monotonic", lambda: next(clock, 500.0))

    with pytest.raises(ProviderError, match="timed out"):
        tasks.await_task(
            lambda method, path, payload: {"id": "v", "status": "processing"},
            {"id": "v", "status": "queued"},
            poll_interval=1,
            timeout=60,
            emit=lambda _msg: None,
        )


def test_await_task_needs_a_task_id():
    with pytest.raises(ProviderError, match="No task id"):
        tasks.await_task(
            lambda method, path, payload: {},
            {},
            poll_interval=0,
            timeout=1,
            emit=lambda _msg: None,
        )
