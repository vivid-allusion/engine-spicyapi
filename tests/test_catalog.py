"""Catalog loader: shipped TOMLs, and the inferred fallback spec."""

import sys
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine_spicyapi.catalog import (  # noqa: E402
    ARRAY_MEDIA_KEYS,
    Catalog,
    EndpointSpec,
    fallback_spec,
    infer_kind,
    infer_prompt_mode,
    load_catalog,
)


def write_toml(tmp_path: Path, category: str, name: str, text: str) -> Path:
    directory = tmp_path / category
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text)
    return path


MINIMAL = """
id = "spicy-test-1"
label = "Spicy Test 1"
category = "IMG"
platform = "spicyapi"
endpoint = "spicy-test-1"
description = "A model that exists only in tests."

[pricing]
base_cost = 0.06
currency = "USD"

[general]
kind = "image"
prompt = "required"
image_url_param = "image_url"

[params.n]
label = "Images per Call"
type = "integer"
default = 1
"""


def test_loads_a_toml_into_a_spec(tmp_path):
    write_toml(tmp_path, "IMG-Models", "spicy-test-1.toml", MINIMAL)
    catalog = load_catalog(tmp_path)

    assert len(catalog) == 1
    spec = catalog.get("spicy-test-1")
    assert spec.kind == "image"
    assert spec.prompt_required
    assert spec.image_url_param == "image_url"
    assert spec.param_type("n") == "integer"
    assert catalog.models() == ["spicy-test-1"]


def test_unknown_model_falls_back_to_an_inferred_spec():
    catalog = Catalog()
    spec = catalog.get("spicy-image-edit-1")

    assert spec.kind == "image-edit"
    assert spec.model == "spicy-image-edit-1"
    assert spec.image_url_param == "image_url"
    assert spec.prompt_required


def test_broken_toml_is_skipped_not_raised(tmp_path):
    write_toml(tmp_path, "IMG-Models", "good.toml", MINIMAL)
    write_toml(tmp_path, "IMG-Models", "broken.toml", "id = [unclosed")
    catalog = load_catalog(tmp_path)

    assert len(catalog) == 1


def test_gitkeep_and_empty_category_dirs_are_fine(tmp_path):
    (tmp_path / "TXT-Models").mkdir(parents=True)
    (tmp_path / "TXT-Models" / ".gitkeep").write_text("")
    assert len(load_catalog(tmp_path)) == 0


@pytest.mark.parametrize(
    ("model", "kind"),
    [
        ("spicy-image-1", "image"),
        ("spicy-image-1-pro", "image"),
        ("spicy-image-photo-1", "image"),
        ("spicy-image-action-1", "image"),
        ("spicy-image-reference-1", "image"),
        ("spicy-image-edit-1", "image-edit"),
        ("spicy-upscale-1", "image-edit"),
        ("spicy-motion-3", "video"),
        ("spicy-motion-3-fast", "video"),
        ("spicy-motion-draft-1", "video"),
        ("spicy-motion-2", "video"),
        ("spicy-motion-1", "video"),
        ("spicy-cinema-1", "video"),
        ("spicy-cinema-1-image", "video"),
        ("spicy-cinema-1-character", "video"),
        ("spicy-video-1", "video"),
        ("spicy-character-video-1", "video"),
        ("spicy-video-edit-1", "video-edit"),
        ("spicy-cinema-1-edit", "video-edit"),
        ("spicy-animate-1", "video-edit"),
    ],
)
def test_inferred_kind_matches_the_documented_family(model, kind):
    assert infer_kind(model) == kind


@pytest.mark.parametrize(
    ("kind", "model", "mode"),
    [
        ("image", "spicy-image-1", "required"),
        ("image", "spicy-image-action-1", "none"),
        ("image", "spicy-image-reference-1", "none"),
        ("image-edit", "spicy-upscale-1", "none"),
        ("video", "spicy-motion-3", "optional"),
        ("video-edit", "spicy-animate-1", "none"),
        ("video-edit", "spicy-video-edit-1", "required"),
    ],
)
def test_inferred_prompt_mode(kind, model, mode):
    assert infer_prompt_mode(kind, model) == mode


def test_array_media_keys():
    spec = EndpointSpec(id="x", model="x", image_url_param="image_urls")
    assert spec.array_media("reference_image_urls")
    assert spec.array_media("audio_urls")
    assert not spec.array_media("image_url")
    assert not spec.array_media("last_frame_url")
    assert "image_urls" in ARRAY_MEDIA_KEYS


def test_array_primary_is_single_when_the_toml_says_so():
    spec = EndpointSpec(
        id="x", model="x", image_url_param="image_urls", single_url_param="image_url"
    )
    assert spec.array_media("image_urls")
    assert not spec.array_media("image_url")


def test_fallback_spec_for_an_unknown_video_model():
    spec = fallback_spec("spicy-something-new-1")
    assert spec.kind == "video"
    assert spec.prompt_never is False
    assert spec.prompt_required is False


def test_standby_profiles_are_packaged():
    import engine_spicyapi

    img = engine_spicyapi.list_standby_profiles("IMG")
    vid = engine_spicyapi.list_standby_profiles("VID")
    assert img and vid
    assert all(path.parent.name == "IMG" for path in img)
    assert all(path.parent.name == "VID" for path in vid)
    assert engine_spicyapi.list_standby_profiles("AUD") == []


def test_shipped_catalog_parses():
    catalog = load_catalog()
    assert len(catalog) >= 20
    for model in catalog.models():
        spec = catalog.get(model)
        assert spec.kind in ("image", "image-edit", "video", "video-edit")
        assert spec.prompt_mode in ("required", "optional", "none")


def test_tomllib_is_available():
    assert tomllib is not None
