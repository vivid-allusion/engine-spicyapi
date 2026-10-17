"""Artifact saving: extension resolution from the bytes that arrived."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine_spicyapi import outputs  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"
JPEG = b"\xff\xd8\xff\xe0" + b"pixels"
GIF = b"GIF89a" + b"pixels"
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 "
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"pixels"
MOV = b"\x00\x00\x00\x14ftypqt  " + b"pixels"


@pytest.mark.parametrize(
    ("data", "expected"),
    [(PNG, ".png"), (JPEG, ".jpg"), (GIF, ".gif"), (WEBP, ".webp"), (MP4, ".mp4"), (MOV, ".mp4")],
)
def test_sniff_extension_reads_the_magic_bytes(data, expected):
    assert outputs.sniff_extension(data, "image") == expected


def test_sniff_extension_falls_back_to_the_media_type():
    assert outputs.sniff_extension(b"unrecognised", "video") == ".mp4"
    assert outputs.sniff_extension(b"unrecognised", "image") == ".png"


def test_default_extension_honours_a_profile_output_format():
    assert outputs.default_extension({"parameters": {"output_format": ".PNG"}}, "image") == ".png"


def test_default_extension_ignores_a_nonsense_format():
    assert outputs.default_extension({"parameters": {"output_format": "image/png"}}, "image") == ".png"


def test_default_extension_for_video():
    assert outputs.default_extension({}, "video") == ".mp4"
    assert outputs.default_extension({"parameters": {"output_format": "mov"}}, "video") == ".mov"


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


def test_save_results_reports_progress(tmp_path):
    downloaded, saved = [], []
    outputs.save_results(
        RecordingClient(),
        tmp_path,
        ["u1", "u2"],
        "image",
        "shot",
        0,
        on_download=lambda: downloaded.append(1),
        on_saved=saved.append,
    )
    assert len(downloaded) == 2
    assert len(saved) == 2
