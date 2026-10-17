"""The endpoint TOML catalog: loading, shapes, and the unknown-model fallback."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine_spicyapi.catalog import (  # noqa: E402
    Catalog,
    EndpointSpec,
    fallback_spec,
    infer_kind,
    load_catalog,
)

TOML = """
id = "wan-3-0-image-to-video"
label = "Wan 3.0 · Image To Video"
category = "VID"
platform = "spicyapi"
endpoint = "alibaba/wan-3.0/image-to-video"
description = "Test video model."

[pricing]
cost_per_second = 0.045
currency = "USD"

[general]
kind = "video"
prompt = "optional"
image_url_param = "image_url"
slots = ["last_image_url", "reference_image_urls"]
media_lists = ["reference_image_urls"]
duration_param_name = "duration_seconds"

[params.duration_seconds]
label = "Duration"
type = "integer"
default = 5

[params.generate_audio]
label = "Generate Audio"
type = "boolean"
default = true

[params.tags]
label = "Tags"
type = "text"
list = true
"""


def write(tmp_path: Path, name: str, text: str) -> Path:
    directory = tmp_path / "VID-Models"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text)
    return path


def test_loads_specs_from_toml(tmp_path):
    write(tmp_path, "wan.toml", TOML)
    catalog = load_catalog(tmp_path)
    assert len(catalog) == 1
    spec = catalog.get("alibaba/wan-3.0/image-to-video")
    assert spec.id == "wan-3-0-image-to-video"
    assert spec.category == "VID"
    assert spec.kind == "video"
    assert spec.prompt_mode == "optional"
    assert spec.image_url_param == "image_url"
    assert spec.slots == ("last_image_url", "reference_image_urls")
    assert spec.media_lists == ("reference_image_urls",)
    assert spec.duration_param_name == "duration_seconds"
    assert spec.known is True


def test_spec_helpers(tmp_path):
    write(tmp_path, "wan.toml", TOML)
    spec = load_catalog(tmp_path).get("alibaba/wan-3.0/image-to-video")
    assert spec.array_media("reference_image_urls") is True
    assert spec.array_media("image_url") is False
    assert spec.array_media("last_image_url") is False
    assert spec.declares("duration_seconds") is True
    assert spec.declares("prompt") is True
    assert spec.declares("seed") is False
    assert spec.param_type("duration_seconds") == "integer"
    assert spec.param_type("generate_audio") == "boolean"
    assert spec.is_list_param("tags") is True
    assert spec.is_list_param("generate_audio") is False
    assert spec.prompt_required is False
    assert spec.prompt_never is False


def test_prompt_modes():
    assert EndpointSpec(id="a", model="a", prompt_mode="required").prompt_required is True
    assert EndpointSpec(id="a", model="a", prompt_mode="none").prompt_never is True


def test_broken_and_idless_tomls_are_skipped(tmp_path):
    write(tmp_path, "broken.toml", "this is not toml = =")
    write(tmp_path, "idless.toml", 'label = "no id"')
    catalog = load_catalog(tmp_path)
    assert len(catalog) == 0
    assert catalog.models() == []


def test_first_spec_wins_per_model(tmp_path):
    write(tmp_path, "a.toml", TOML.replace("wan-3-0-image-to-video", "first"))
    write(tmp_path, "b.toml", TOML.replace("wan-3-0-image-to-video", "second"))
    catalog = load_catalog(tmp_path)
    assert len(catalog) == 1
    assert catalog.get("alibaba/wan-3.0/image-to-video").id == "first"


def test_unknown_model_falls_back_without_dropping_anything():
    catalog = Catalog()
    spec = catalog.get("someone/unknown-model/edit")
    assert spec.known is False
    assert spec.model == "someone/unknown-model/edit"
    assert spec.image_url_param == "image_url"
    assert spec.prompt_mode == "optional"


def test_infer_kind():
    assert infer_kind("x/image-upscaler-v1/upscale") == "image"
    assert infer_kind("x/text-to-image") == "image"
    assert infer_kind("alibaba/wan-3.0/text-to-video") == "video"


def test_fallback_spec_is_marked_unknown():
    spec = fallback_spec("a/b", "VID")
    assert spec.known is False
    assert spec.category == "VID"
    assert spec.label == "a/b"


def test_missing_endpoints_dir_is_not_an_error(tmp_path):
    catalog = load_catalog(tmp_path / "nope")
    assert len(catalog) == 0
