"""The live-schema drift gate — tested offline.

The gate's whole job is deciding whether the TOML catalog still mirrors the
provider's published request schemas, so its logic is tested without network
access: a pinned copy of the four endpoints' property sets (taken from
https://api.spicyapi.com/openapi.json and the provider's `.md` API reference,
which agree) plus the real shipped catalog.

If the provider adds or renames an input, `scripts/check_schema_sync.py`
against the live schema reports it; if the catalog drifts, these tests report
it. Either way it fails loudly at PR time instead of at run time.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine_spicyapi.catalog import CATEGORY_DIRS, ENDPOINTS_DIR  # noqa: E402


def load_gate():
    spec = importlib.util.spec_from_file_location(
        "check_schema_sync", ROOT / "scripts" / "check_schema_sync.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load_gate()

#: Pinned from the provider's OpenAPI document (== its `.md` API reference).
PINNED_PROPERTIES = {
    "image": [
        "model", "prompt", "reference_image", "action", "image_url",
        "negative_prompt", "enhance_prompt", "enhance_mode", "size", "n", "seed",
        "character", "views", "style", "dry_run", "user",
    ],
    "image-edit": [
        "model", "prompt", "image_url", "image_urls", "negative_prompt",
        "enhance_prompt", "size", "n", "seed", "character", "views", "mask_url",
        "dry_run", "user",
    ],
    "video": [
        "model", "prompt", "image_url", "last_frame_url", "reference_image_urls",
        "character", "views", "action", "style", "video_url", "audio_url",
        "audio_urls", "audio_mode", "resolution", "aspect_ratio", "duration",
        "negative_prompt", "enhance_prompt", "audio", "fps", "seed", "dry_run",
        "user",
    ],
    "video-edit": [
        "model", "video_url", "prompt", "reference_image_urls", "resolution",
        "aspect_ratio", "keep_audio", "negative_prompt", "seed", "enhance_prompt",
        "image_url", "motion", "quality", "dry_run", "user",
    ],
}


def fixture_schema() -> dict:
    paths = {}
    for kind, (path, method) in gate.ENDPOINT_BY_KIND.items():
        paths[path] = {
            method: {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "properties": {name: {} for name in PINNED_PROPERTIES[kind]}
                            }
                        }
                    }
                }
            }
        }
    return {"paths": paths}


def toml(tmp_path, name, kind, params=(), slots=(), **general):
    directory = tmp_path / "IMG-Models"
    directory.mkdir(exist_ok=True)
    body = "".join(f"[params.{key}]\ntype = \"text\"\n" for key in params)
    slot_list = ", ".join(f'"{slot}"' for slot in slots)
    general_line = ", ".join(f'{key} = "{value}"' for key, value in general.items())
    text = f"""
id = "{name}"
label = "{name}"
category = "IMG"
platform = "spicyapi"
endpoint = "{name}"
description = "fixture"

[general]
kind = "{kind}"
prompt = "required"
{general_line}
slots = [{slot_list}]

{body}
"""
    path = directory / f"{name}.toml"
    path.write_text(text)
    return path


# ── unit: the pure pieces ──────────────────────────────────────────────────


def test_endpoint_properties_reads_the_request_body():
    schema = fixture_schema()
    assert gate.endpoint_properties(schema, "/v1/images/generations") == set(
        PINNED_PROPERTIES["image"]
    )


def test_declared_keys_include_params_slots_and_media_keys(tmp_path):
    path = toml(
        tmp_path,
        "m",
        "video",
        params=["duration", "seed"],
        slots=["reference_image_urls"],
        image_url_param="image_url",
    )
    data = gate.tomllib.loads(path.read_text())
    assert gate.declared_keys(data) == {
        "duration",
        "seed",
        "reference_image_urls",
        "image_url",
    }


def test_diff_reports_missing_and_extra():
    missing, extra = gate.diff({"a", "invented"}, {"a", "b", "model", "prompt"}, set())
    assert missing == {"b"}
    assert extra == {"invented"}


def test_documented_toml_only_keys_are_tolerated():
    missing, extra = gate.diff({"scale"}, {"prompt", "model"}, {"scale"})
    assert not extra
    assert "scale" in gate.TOML_ONLY


# ── unit: findings ────────────────────────────────────────────────────────


def test_omitted_property_is_reported_as_coverage_drift(tmp_path):
    toml(tmp_path, "m", "image", params=["n"])
    findings = gate.check(fixture_schema(), gate.load_tomls(tmp_path))
    assert any("accepted by the server but declared by no image TOML" in f for f in findings)


def test_invented_property_is_reported(tmp_path):
    toml(tmp_path, "m", "image", params=["n", "not_a_real_param", "size", "seed",
                                         "character", "views", "style", "dry_run",
                                         "user", "negative_prompt", "enhance_prompt",
                                         "enhance_mode", "action", "reference_image"])
    findings = gate.check(fixture_schema(), gate.load_tomls(tmp_path))
    assert any("not_a_real_param" in f for f in findings)


def test_unknown_kind_is_reported(tmp_path):
    toml(tmp_path, "m", "img")
    findings = gate.check(fixture_schema(), gate.load_tomls(tmp_path))
    assert any("unknown or missing [general] kind" in f for f in findings)


# ── end to end: the real catalog against the pinned schema ────────────────


def test_shipped_catalog_mirrors_the_pinned_schema():
    findings = gate.check(fixture_schema(), gate.load_tomls(ENDPOINTS_DIR))
    assert findings == [], "\n".join(findings)


def test_every_category_dir_exists():
    for dir_name in CATEGORY_DIRS.values():
        assert (ENDPOINTS_DIR / dir_name).is_dir()


def test_catalog_has_image_and_video_models():
    tomls = gate.load_tomls(ENDPOINTS_DIR)
    kinds = {gate.kind_of(data) for _path, data in tomls}
    assert {"image", "image-edit", "video", "video-edit"} <= kinds


@pytest.mark.parametrize("kind", ["image", "image-edit", "video", "video-edit"])
def test_each_kind_has_at_least_one_model(kind):
    kinds = [gate.kind_of(data) for _path, data in gate.load_tomls(ENDPOINTS_DIR)]
    assert kinds.count(kind) >= 1
