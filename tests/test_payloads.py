"""Request-input construction: params, prompt, media routing, duration."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine_spicyapi.catalog import EndpointSpec  # noqa: E402
from engine_spicyapi.datatypes import EngineError, InputFile  # noqa: E402
from engine_spicyapi.payloads import (  # noqa: E402
    build_input,
    compose_prompt,
    encode_param,
    is_unset,
    primary_key,
    shape_media,
)

PROMPT = Path("markdown-001.md")

VIDEO = EndpointSpec(
    id="wan-3-0-image-to-video",
    model="alibaba/wan-3.0/image-to-video",
    kind="video",
    image_url_param="image_url",
    slots=("last_image_url", "reference_image_urls"),
    media_lists=("reference_image_urls",),
    prompt_mode="optional",
    params={
        "duration_seconds": {"type": "integer", "default": 5},
        "generate_audio": {"type": "boolean", "default": True},
        "resolution": {"type": "select", "default": "720p"},
        "tags": {"type": "text", "list": True},
    },
)

TEXT_ONLY = EndpointSpec(
    id="seedream-t2i",
    model="bytedance/seedream-5.0-pro/text-to-image",
    kind="image",
    prompt_mode="required",
    params={"resolution": {"type": "select", "default": "2k"}},
)


def item(**kwargs) -> InputFile:
    base = dict(path=PROMPT, prompt="a paper lantern at night")
    base.update(kwargs)
    return InputFile(**base)


def test_compose_prompt_order():
    profile = {"prompt_prefix": "PREFIX ", "prompt_suffix": " SUFFIX"}
    assert compose_prompt(profile, item()) == "PREFIX a paper lantern at night SUFFIX"


def test_compose_prompt_strips_when_bare():
    assert compose_prompt({}, item(prompt="  hi  ")) == "hi"


def test_basic_input_carries_model_fields_and_prompt():
    payload, warnings = build_input(
        {}, VIDEO, {"duration_seconds": 8, "generate_audio": False}, "hello", item()
    )
    assert warnings == []
    assert payload == {"duration_seconds": 8, "generate_audio": False, "prompt": "hello"}


def test_unset_values_are_omitted():
    payload, _ = build_input(
        {}, VIDEO, {"resolution": "", "generate_audio": None, "tags": []}, "hello", item()
    )
    assert payload == {"prompt": "hello"}


def test_undeclared_profile_param_is_dropped_with_a_warning():
    payload, warnings = build_input({}, VIDEO, {"fps": 24}, "hello", item())
    assert "fps" not in payload
    assert len(warnings) == 1
    assert "does not declare 'fps'" in warnings[0]


def test_unknown_model_keeps_every_param():
    payload, warnings = build_input({}, EndpointSpec(id="x", model="a/b", known=False), {"fps": 24}, "hi", item())
    assert payload["fps"] == 24
    assert warnings == []


def test_coercion_follows_the_declared_type():
    payload, _ = build_input(
        {},
        VIDEO,
        {"duration_seconds": "12", "generate_audio": "false", "tags": "a, b ,c"},
        "hi",
        item(),
    )
    assert payload["duration_seconds"] == 12
    assert payload["generate_audio"] is False
    assert payload["tags"] == ["a", "b", "c"]


def test_primary_media_is_routed_to_the_models_key():
    payload, _ = build_input({}, VIDEO, {}, "hi", item(reference_urls=["https://cdn/x.png"]))
    assert payload["image_url"] == "https://cdn/x.png"


def test_array_media_takes_every_url():
    payload, _ = build_input(
        {},
        VIDEO,
        {},
        "hi",
        item(references={"reference_image_urls": ["https://cdn/a.png", "https://cdn/b.png"]}),
    )
    assert payload["reference_image_urls"] == ["https://cdn/a.png", "https://cdn/b.png"]


def test_single_url_slot_refuses_to_drop_media():
    with pytest.raises(EngineError) as excinfo:
        build_input(
            {},
            VIDEO,
            {},
            "hi",
            item(references={"last_image_url": ["https://cdn/a.png", "https://cdn/b.png"]}),
        )
    assert "accepts a single media URL" in str(excinfo.value)


def test_media_on_a_text_only_model_is_refused_loudly():
    with pytest.raises(EngineError) as excinfo:
        build_input({}, TEXT_ONLY, {}, "hi", item(reference_urls=["https://cdn/x.png"]))
    assert "takes no media input" in str(excinfo.value)


def test_text_only_model_sends_no_media_key():
    payload, _ = build_input({}, TEXT_ONLY, {"resolution": "1k"}, "hi", item())
    assert payload == {"resolution": "1k", "prompt": "hi"}


def test_required_slot_missing_is_refused():
    spec = EndpointSpec(
        id="lip",
        model="spicyapi/lip-sync-v1/lip-sync",
        kind="video",
        image_url_param="video_url",
        slots=("audio_url",),
        required_slots=("audio_url",),
        prompt_mode="none",
        params={},
    )
    with pytest.raises(EngineError) as excinfo:
        build_input({}, spec, {}, "", item())
    assert "requires the 'audio_url' input" in str(excinfo.value)


def test_required_slot_supplied_is_sent():
    spec = EndpointSpec(
        id="lip",
        model="spicyapi/lip-sync-v1/lip-sync",
        kind="video",
        image_url_param="video_url",
        slots=("audio_url",),
        required_slots=("audio_url",),
        prompt_mode="none",
        params={},
    )
    payload, _ = build_input(
        {},
        spec,
        {},
        "",
        item(reference_urls=["https://cdn/v.mp4"], references={"audio_url": ["https://cdn/a.mp3"]}),
    )
    assert payload == {"video_url": "https://cdn/v.mp4", "audio_url": "https://cdn/a.mp3"}


def test_duration_from_metadata_lands_on_the_models_key():
    payload, warnings = build_input({}, VIDEO, {}, "hi", item(metadata={"duration": "7"}))
    assert payload["duration_seconds"] == 7
    assert warnings == []


def test_duration_on_a_model_without_one_is_dropped_with_a_warning():
    payload, warnings = build_input({}, TEXT_ONLY, {}, "hi", item(metadata={"duration": 5}))
    assert "duration_seconds" not in payload
    assert len(warnings) == 1
    assert "takes no 'duration_seconds' input" in warnings[0]


def test_prompt_never_models_get_no_prompt():
    spec = EndpointSpec(id="up", model="spicyapi/image-upscaler-v1/upscale", prompt_mode="none")
    payload, _ = build_input({}, spec, {}, "ignored", item())
    assert "prompt" not in payload


def test_profile_image_url_param_overrides_the_catalog():
    profile = {"image_url_param": "first_frame"}
    assert primary_key(profile, VIDEO) == "first_frame"


def test_primary_key_defaults_to_image_url():
    assert primary_key({}, EndpointSpec(id="x", model="y")) == "image_url"


def test_is_unset():
    assert is_unset(None) is True
    assert is_unset("") is True
    assert is_unset("   ") is True
    assert is_unset([]) is True
    assert is_unset({}) is True
    assert is_unset(0) is False
    assert is_unset(False) is False
    assert is_unset("0") is False


def test_encode_param_keeps_unknown_types_as_is():
    spec = EndpointSpec(id="x", model="y")
    assert encode_param("seed", 42, spec) == 42
    assert encode_param("seed", "42", spec) == "42"


def test_shape_media_empty_returns_empty_list():
    assert shape_media("image_url", [], VIDEO) == []
