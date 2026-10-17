"""The shipped catalog: TOML integrity and every standby profile runs.

No network. These tests prove the repo is internally consistent: every TOML
parses and declares what the Engine and the wizard read, and every published
profile builds a real request through the real payload builder.
"""

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine_spicyapi import list_standby_profiles  # noqa: E402
from engine_spicyapi.catalog import CATEGORY_DIRS, ENDPOINTS_DIR, load_catalog  # noqa: E402
from engine_spicyapi.datatypes import InputFile  # noqa: E402
from engine_spicyapi.payloads import build_input  # noqa: E402

ALLOWED_TYPES = {"select", "integer", "number", "boolean", "text"}
KNOWN_GENERAL = {
    "kind",
    "prompt",
    "image_url_param",
    "slots",
    "required_slots",
    "media_lists",
    "duration_param_name",
}

CATALOG = load_catalog()


def toml_paths():
    out = []
    for category, dir_name in CATEGORY_DIRS.items():
        directory = ENDPOINTS_DIR / dir_name
        if directory.is_dir():
            out.extend((category, path) for path in sorted(directory.glob("*.toml")))
    return out


def profile_paths():
    return sorted(list_standby_profiles())


def test_the_shipped_catalog_is_not_empty():
    assert len(CATALOG) == 24
    assert len(CATALOG.models()) == len(set(CATALOG.models()))


def test_every_endpoint_is_reachable_by_its_own_id():
    for model in CATALOG.models():
        spec = CATALOG.get(model)
        assert spec.model == model
        assert spec.id
        assert spec.label
        assert spec.kind in {"image", "video"}


@pytest.mark.parametrize(("category", "path"), toml_paths(), ids=lambda value: getattr(value, "name", value))
def test_toml_declares_what_the_ecosystem_reads(category, path):
    import tomllib

    with open(path, "rb") as handle:
        data = tomllib.load(handle)

    assert data["category"] == category
    assert data["platform"] == "spicyapi"
    assert data["endpoint"]
    assert data.get("description")
    pricing = data.get("pricing") or {}
    assert "base_cost" in pricing or "cost_per_second" in pricing
    assert pricing.get("currency") == "USD"

    general = data.get("general") or {}
    assert set(general) <= KNOWN_GENERAL
    assert general.get("kind") == ("image" if category == "IMG" else "video")
    assert general.get("prompt") in {"required", "optional", "none"}

    for key, param in (data.get("params") or {}).items():
        assert param.get("label"), key
        assert param.get("type") in ALLOWED_TYPES, key
        if param.get("type") == "select":
            assert param.get("options"), key
            for option in param["options"]:
                assert "value" in option and "label" in option, key


@pytest.mark.parametrize(("category", "path"), toml_paths(), ids=lambda value: getattr(value, "name", value))
def test_toml_id_matches_its_filename(category, path):
    import tomllib

    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    assert path.stem == data["id"]


def test_primary_media_key_is_not_also_a_slot():
    for model in CATALOG.models():
        spec = CATALOG.get(model)
        assert spec.image_url_param not in spec.slots


def test_pinned_catalog_facts():
    """The shape of a few models, pinned so a silent catalog edit is visible."""
    t2i = CATALOG.get("bytedance/seedream-5.0-pro/text-to-image")
    assert t2i.kind == "image"
    assert t2i.prompt_mode == "required"
    assert t2i.image_url_param == ""
    assert t2i.slots == ()

    edit = CATALOG.get("bytedance/seedream-5.0-pro/edit")
    assert edit.image_url_param == "image_urls"
    assert edit.array_media("image_urls") is True

    i2v = CATALOG.get("alibaba/wan-3.0/image-to-video")
    assert i2v.kind == "video"
    assert i2v.prompt_mode == "optional"
    assert i2v.image_url_param == "image_url"
    assert i2v.slots == ("last_image_url",)
    assert i2v.duration_param_name == "duration_seconds"

    extend = CATALOG.get("alibaba/wan-3.0/video-extend")
    assert extend.image_url_param == "video_url"

    upscale = CATALOG.get("spicyapi/image-upscaler-v1/upscale")
    assert upscale.prompt_mode == "none"
    assert upscale.image_url_param == "image_url"


def test_standby_profiles_exist_for_both_shelves():
    image = list_standby_profiles("IMG")
    video = list_standby_profiles("VID")
    assert len(image) >= 10
    assert len(video) >= 10
    assert len(image) + len(video) == len(profile_paths())


@pytest.mark.parametrize("path", profile_paths(), ids=lambda value: value.name)
def test_every_standby_profile_parses_and_points_at_a_known_model(path):
    profile = yaml.safe_load(path.read_text())
    assert profile["platform"] == "spicyapi"
    assert profile["endpoint"] in CATALOG.models(), profile["endpoint"]
    assert profile["media_type"] in {"image", "video"}
    assert isinstance(profile.get("parameters"), dict)
    assert profile.get("pricing")


@pytest.mark.parametrize("path", profile_paths(), ids=lambda value: value.name)
def test_every_standby_profile_matches_its_shelf(path):
    profile = yaml.safe_load(path.read_text())
    spec = CATALOG.get(profile["endpoint"])
    assert spec.kind == profile["media_type"]
    assert path.parent.name == ("IMG" if spec.kind == "image" else "VID")


@pytest.mark.parametrize("path", profile_paths(), ids=lambda value: value.name)
def test_every_standby_profile_only_sets_declared_params(path):
    profile = yaml.safe_load(path.read_text())
    spec = CATALOG.get(profile["endpoint"])
    for key in profile["parameters"]:
        assert spec.declares(key), f"{path.name}: {key} is not declared by {spec.model}"


@pytest.mark.parametrize("path", profile_paths(), ids=lambda value: value.name)
def test_every_standby_profile_builds_a_request(path):
    profile = yaml.safe_load(path.read_text())
    spec = CATALOG.get(profile["endpoint"])
    refs = ["https://cdn.example/ref.png"] if spec.image_url_param else []
    item = InputFile(path=Path("markdown-001.md"), prompt="a test prompt", reference_urls=refs)

    payload, warnings = build_input(
        profile, spec, dict(profile["parameters"]), "a test prompt", item
    )

    assert warnings == [], f"{path.name}: {warnings}"
    if spec.image_url_param:
        assert payload[spec.image_url_param]
    if spec.prompt_mode == "none":
        assert "prompt" not in payload
    else:
        assert payload["prompt"] == "a test prompt"
    for key in payload:
        assert key == "prompt" or spec.declares(key), f"{path.name}: {key} not declared"
