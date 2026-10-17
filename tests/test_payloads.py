"""Payload construction — pure functions, no network."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine_spicyapi import EngineError, InputFile  # noqa: E402
from engine_spicyapi.catalog import EndpointSpec  # noqa: E402
from engine_spicyapi.payloads import (  # noqa: E402
    build_payload,
    compose_prompt,
    encode_param,
    primary_key,
    shape_media,
)


def spec(**overrides) -> EndpointSpec:
    base = dict(
        id="spicy-image-1",
        model="spicy-image-1",
        kind="image",
        prompt_mode="required",
        image_url_param="image_url",
        params={
            "n": {"type": "integer"},
            "size": {"type": "select"},
            "enhance_prompt": {"type": "boolean"},
            "enhance_mode": {"type": "select"},
            "character": {"type": "text"},
            "views": {"type": "text", "list": True},
            "duration": {"type": "integer"},
            "fps": {"type": "select"},
        },
    )
    base.update(overrides)
    return EndpointSpec(**base)


def item(**overrides) -> InputFile:
    base = dict(path=Path("prompts/shot-001.md"), prompt="a woman in a red dress")
    base.update(overrides)
    return InputFile(**base)


def test_compose_prompt_applies_prefix_and_suffix():
    profile = {"prompt_prefix": "[style] ", "prompt_suffix": " [8k]"}
    assert compose_prompt(profile, item()) == "[style] a woman in a red dress [8k]"


def test_primary_key_precedence():
    assert primary_key({"image_url_param": "image"}, spec()) == "image"
    assert primary_key({}, spec()) == "image_url"
    assert primary_key({}, spec(image_url_param="")) == "image_url"


def test_payload_carries_model_prompt_and_params():
    payload, warnings = build_payload(
        {}, spec(), {"n": "2", "size": "1024*1024"}, "a prompt", item()
    )
    assert payload["model"] == "spicy-image-1"
    assert payload["prompt"] == "a prompt"
    assert payload["n"] == 2  # integer-typed, coerced from the profile's string
    assert payload["size"] == "1024*1024"
    assert warnings == []


def test_prompt_is_omitted_when_empty():
    payload, _ = build_payload({}, spec(), {}, "", item())
    assert "prompt" not in payload


def test_prompt_is_never_sent_to_a_model_that_rejects_it():
    payload, _ = build_payload({}, spec(prompt_mode="none"), {}, "ignored", item())
    assert "prompt" not in payload


def test_generator_fps_is_dropped_with_a_warning():
    payload, warnings = build_payload({}, spec(), {"fps": 24}, "p", item())
    assert "fps" not in payload
    assert warnings and "24" in warnings[0]


def test_sixty_fps_is_forwarded_as_an_integer():
    payload, warnings = build_payload({}, spec(), {"fps": "60"}, "p", item())
    assert payload["fps"] == 60
    assert warnings == []


def test_media_routing_to_the_primary_key():
    payload, _ = build_payload(
        {}, spec(), {}, "p", item(reference_urls=["https://cdn.example/a.jpg"])
    )
    assert payload["image_url"] == "https://cdn.example/a.jpg"


def test_named_reference_slots_are_routed_separately():
    payload, _ = build_payload(
        {},
        spec(slots=("last_frame_url",)),
        {},
        "p",
        item(references={"last_frame_url": ["https://cdn.example/last.jpg"]}),
    )
    assert payload["last_frame_url"] == "https://cdn.example/last.jpg"
    assert "image_url" not in payload


def test_declared_slots_are_emitted_empty_when_required():
    payload, _ = build_payload(
        {}, spec(required_slots=("audio_urls",)), {}, "p", item(references={"audio_urls": []})
    )
    assert payload["audio_urls"] == []


def test_array_slots_keep_the_full_list():
    payload, _ = build_payload(
        {},
        spec(slots=("reference_image_urls",)),
        {},
        "p",
        item(references={"reference_image_urls": ["u1", "u2", "u3"]}),
    )
    assert payload["reference_image_urls"] == ["u1", "u2", "u3"]


def test_single_url_slot_refuses_to_drop_extra_media():
    with pytest.raises(EngineError) as excinfo:
        build_payload(
            {},
            spec(slots=("last_frame_url",)),
            {},
            "p",
            item(references={"last_frame_url": ["u1", "u2"]}),
        )
    assert "accepts a single media URL" in str(excinfo.value)


def test_edit_models_use_the_single_url_alias_for_one_image():
    edit = spec(
        model="spicy-image-edit-1",
        kind="image-edit",
        image_url_param="image_urls",
        single_url_param="image_url",
    )
    one, _ = build_payload({}, edit, {}, "p", item(reference_urls=["u1"]))
    many, _ = build_payload({}, edit, {}, "p", item(reference_urls=["u1", "u2"]))
    assert one["image_url"] == "u1"
    assert "image_urls" not in one
    assert many["image_urls"] == ["u1", "u2"]
    assert "image_url" not in many


def test_duration_metadata_overrides_the_profile_value():
    payload, _ = build_payload(
        {"duration_param_name": "duration"},
        spec(kind="video", prompt_mode="optional", duration_param_name="duration"),
        {"duration": 8},
        "p",
        item(metadata={"duration": "5"}),
    )
    assert payload["duration"] == 5


def test_no_duration_input_means_the_metadata_duration_is_dropped_with_a_warning():
    edit = spec(
        model="spicy-cinema-1-edit",
        kind="video-edit",
        prompt_mode="required",
        duration_type="",
        params={},
    )
    payload, warnings = build_payload(
        {"duration_param_name": "duration"}, edit, {}, "p", item(metadata={"duration": 5})
    )
    assert "duration" not in payload
    assert warnings and "no 'duration' input" in warnings[0]


def test_unknown_models_keep_the_profile_duration():
    unknown = EndpointSpec(
        id="spicy-something-new", model="spicy-something-new", kind="video", known=False
    )
    payload, _ = build_payload({}, unknown, {}, "p", item(metadata={"duration": 5}))
    assert payload["duration"] == 5


def test_smart_duration_token_passes_through():
    payload, _ = build_payload(
        {},
        spec(kind="video", prompt_mode="optional", duration_param_name="duration"),
        {},
        "p",
        item(metadata={"duration": "-1"}),
    )
    assert payload["duration"] == -1


def test_enhance_mode_is_pruned_without_enhance_prompt():
    payload, _ = build_payload(
        {}, spec(), {"enhance_prompt": False, "enhance_mode": "agent"}, "p", item()
    )
    assert "enhance_mode" not in payload

    payload, _ = build_payload(
        {}, spec(), {"enhance_prompt": True, "enhance_mode": "agent"}, "p", item()
    )
    assert payload["enhance_mode"] == "agent"


def test_views_are_pruned_without_a_character():
    payload, _ = build_payload({}, spec(), {"views": "cv_1,cv_2"}, "p", item())
    assert "views" not in payload


def test_views_split_into_a_list_with_a_character():
    payload, _ = build_payload(
        {}, spec(), {"character": "chr_1", "views": "cv_1, cv_2"}, "p", item()
    )
    assert payload["views"] == ["cv_1", "cv_2"]


def test_generator_plumbing_never_reaches_the_payload():
    payload, _ = build_payload(
        {},
        spec(),
        {"prompt_prefix": "x", "prompt_suffix": "y", "kind": "image", "api_path": "z"},
        "p",
        item(),
    )
    assert set(payload) == {"model", "prompt"}


def test_unset_params_are_not_sent():
    payload, _ = build_payload(
        {},
        spec(),
        {"action": "", "negative_prompt": "", "seed": None, "views": []},
        "p",
        item(),
    )
    assert set(payload) == {"model", "prompt"}


def test_negative_prompt_that_is_really_set_is_kept():
    payload, _ = build_payload({}, spec(), {"negative_prompt": "blurry"}, "p", item())
    assert payload["negative_prompt"] == "blurry"


def test_encode_param_types():
    target = spec()
    assert encode_param("n", "3", target) == 3
    assert encode_param("n", 2.0, target) == 2
    assert encode_param("n", "auto", target) == "auto"
    assert encode_param("enhance_prompt", "true", target) is True
    assert encode_param("enhance_prompt", "off", target) is False
    assert encode_param("unknown", "keep", target) == "keep"


def test_shape_media_returns_a_list_for_arrays_and_a_string_otherwise():
    target = spec(slots=("reference_image_urls",))
    assert shape_media("reference_image_urls", ["u1"], target) == ["u1"]
    assert shape_media("image_url", ["u1"], target) == "u1"
    assert shape_media("image_url", [], target) == []
