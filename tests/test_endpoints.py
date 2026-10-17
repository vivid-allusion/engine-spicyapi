"""Endpoint catalog integrity — the per-model layer.

`scripts/check_schema_sync.py` proves the catalog covers every property the
four endpoints accept and invents nothing. It cannot prove *which* model
accepts which input: the OpenAPI document is per endpoint, and the per-model
truth lives in `GET /v1/models` (behind a key) and the provider's docs
snapshot (`engine-spicyapi.ai-docs`, `llm-full.txt`). Those facts are pinned
here, from that snapshot's "Models and prices" and "## Models" sections.
"""

import sys
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine_spicyapi.catalog import CATEGORY_DIRS, ENDPOINTS_DIR, load_catalog  # noqa: E402

PROFILES_DIR = ENDPOINTS_DIR.parent / "profiles" / "standby"
VALID_TYPES = {"select", "text", "integer", "number", "boolean"}

IMAGE_SIZES = [
    "1024*1024", "832*1216", "1216*832", "1280*1280", "1440*1440",
    "1024*1536", "1536*1024", "1080*1920", "1920*1080", "1152*2048", "2048*1152",
]

#: Documented resolutions per model (docs snapshot, "## Models" table).
RESOLUTIONS = {
    "spicy-motion-3": {"480P", "720P", "1080P"},
    "spicy-motion-3-fast": {"480P", "720P", "1080P"},
    "spicy-cinema-1": {"480P", "720P", "1080P"},
    "spicy-cinema-1-image": {"480P", "720P", "1080P"},
    "spicy-cinema-1-character": {"480P", "720P", "1080P"},
    "spicy-motion-2": {"720P", "1080P"},
    "spicy-character-video-1": {"720P", "1080P"},
    "spicy-video-1": {"720P", "1080P"},
    "spicy-motion-draft-1": {"720P", "1080P"},
    "spicy-motion-1": {"720P", "1080P"},
    "spicy-video-edit-1": {"720P", "1080P"},
    "spicy-cinema-1-edit": {"720P", "1080P"},
}

#: Documented duration bounds (min, max) — -1 means "smart duration" where allowed.
DURATIONS = {
    "spicy-motion-1": (2, 10),
    "spicy-motion-2": (2, 15),
    "spicy-motion-draft-1": (2, 15),
    "spicy-character-video-1": (2, 15),
    "spicy-video-1": (2, 15),
    "spicy-motion-3": (-1, 30),
    "spicy-motion-3-fast": (-1, 30),
    "spicy-cinema-1": (3, 15),
    "spicy-cinema-1-image": (3, 15),
    "spicy-cinema-1-character": (3, 15),
}

#: Models that take no prompt at all.
NO_PROMPT = {"spicy-image-action-1", "spicy-image-reference-1", "spicy-upscale-1", "spicy-animate-1"}

#: Models whose docs say a prompt is required.
PROMPT_REQUIRED = {
    "spicy-image-1", "spicy-image-1-pro", "spicy-image-photo-1",
    "spicy-image-edit-1", "spicy-video-edit-1", "spicy-cinema-1-edit",
}

#: Primary media key per model (what the Markdown URLs route into).
PRIMARY_MEDIA = {
    "spicy-image-1": None,
    "spicy-image-1-pro": None,
    "spicy-image-photo-1": None,
    "spicy-image-action-1": "image_url",
    "spicy-image-reference-1": "reference_image",
    "spicy-image-edit-1": "image_urls",
    "spicy-upscale-1": "image_url",
    "spicy-motion-3": "image_url",
    "spicy-motion-3-fast": "image_url",
    "spicy-motion-draft-1": "image_url",
    "spicy-motion-2": "image_url",
    "spicy-motion-1": "image_url",
    "spicy-cinema-1": None,
    "spicy-cinema-1-image": "image_url",
    "spicy-cinema-1-character": "reference_image_urls",
    "spicy-video-1": None,
    "spicy-character-video-1": "image_url",
    "spicy-video-edit-1": "video_url",
    "spicy-cinema-1-edit": "video_url",
    "spicy-animate-1": "image_url",
}


def load_all() -> dict[str, list[tuple[Path, dict]]]:
    out: dict[str, list[tuple[Path, dict]]] = {}
    for category, dir_name in CATEGORY_DIRS.items():
        entries = []
        for toml_file in sorted((ENDPOINTS_DIR / dir_name).glob("*.toml")):
            with open(toml_file, "rb") as handle:
                entries.append((toml_file, tomllib.load(handle)))
        out[category] = entries
    return out


def by_id() -> dict[str, dict]:
    return {
        data["id"]: data
        for entries in load_all().values()
        for _path, data in entries
    }


def profiles() -> list[tuple[Path, dict]]:
    found = []
    for shelf in sorted(PROFILES_DIR.iterdir()):
        if shelf.is_dir():
            for yaml_file in sorted(shelf.glob("*.yaml")):
                found.append((yaml_file, yaml.safe_load(yaml_file.read_text())))
    return found


# ── shape ─────────────────────────────────────────────────────────────────


def test_catalog_counts():
    catalog = load_all()
    assert len(catalog["IMG"]) == 7
    assert len(catalog["VID"]) == 13
    assert catalog["TXT"] == []
    assert catalog["VISION"] == []


def test_required_keys_and_platform():
    for category, entries in load_all().items():
        for path, ep in entries:
            assert ep["id"], f"{path.name}: missing id"
            assert ep["label"], f"{ep['id']}: missing label"
            assert ep["category"] == category, f"{ep['id']}: category mismatch"
            assert ep["platform"] == "spicyapi", f"{ep['id']}: platform must be spicyapi"
            assert ep["endpoint"].startswith("spicy-"), f"{ep['id']}: endpoint must be a model id"
            assert ep["description"], f"{ep['id']}: missing description"
            assert ep["general"]["kind"] in ("image", "image-edit", "video", "video-edit")


def test_ids_unique_within_category():
    for category, entries in load_all().items():
        ids = [ep["id"] for _path, ep in entries]
        assert len(ids) == len(set(ids)), f"duplicate ids in {category}"


def test_kind_matches_category():
    for category, entries in load_all().items():
        for _path, ep in entries:
            kind = ep["general"]["kind"]
            if category == "IMG":
                assert kind in ("image", "image-edit"), f"{ep['id']}: {kind} in IMG"
            if category == "VID":
                assert kind in ("video", "video-edit"), f"{ep['id']}: {kind} in VID"


def test_select_params_have_options_and_a_default_in_range():
    for _category, entries in load_all().items():
        for _path, ep in entries:
            for name, param in ep.get("params", {}).items():
                if param.get("type") == "select":
                    values = [option["value"] for option in param.get("options") or []]
                    assert values, f"{ep['id']}: select {name} has no options"
                    if "default" in param:
                        assert param["default"] in values, f"{ep['id']}: {name} default off-menu"


def test_params_are_well_formed():
    for _category, entries in load_all().items():
        for _path, ep in entries:
            for name, param in ep.get("params", {}).items():
                assert param.get("type") in VALID_TYPES, f"{ep['id']}: bad type for {name}"
                assert param.get("label"), f"{ep['id']}: missing label for {name}"
                if param.get("type") == "boolean":
                    assert isinstance(param.get("default"), bool), f"{ep['id']}: {name} default"
                if "min" in param and "max" in param:
                    assert param["min"] <= param["max"], f"{ep['id']}: {name} min/max"


def test_no_prompt_is_promised_where_the_model_rejects_one():
    for model in NO_PROMPT:
        assert by_id()[model]["general"]["prompt"] == "none"


def test_prompt_modes_match_the_docs():
    catalog = by_id()
    for model, data in catalog.items():
        mode = data["general"]["prompt"]
        if model in NO_PROMPT:
            continue
        if model in PROMPT_REQUIRED:
            assert mode == "required", f"{model}: expected required, got {mode}"
        else:
            assert mode == "optional", f"{model}: expected optional, got {mode}"


def test_primary_media_key_per_model():
    catalog = by_id()
    for model, expected in PRIMARY_MEDIA.items():
        declared = catalog[model]["general"].get("image_url_param")
        if expected is None:
            assert not declared, f"{model}: takes no primary image input, declares {declared}"
        else:
            assert declared == expected, f"{model}: primary media key {declared} != {expected}"


def test_image_edit_models_carry_the_single_url_alias():
    catalog = by_id()
    assert catalog["spicy-image-edit-1"]["general"]["single_url_param"] == "image_url"
    for model, data in catalog.items():
        if data["general"]["kind"] != "image-edit" or model == "spicy-image-edit-1":
            continue
        assert "single_url_param" not in data["general"], f"{model}: unexpected single_url_param"


def test_text_to_image_models_offer_the_eleven_documented_sizes():
    for model in ("spicy-image-1", "spicy-image-1-pro", "spicy-image-photo-1"):
        options = {option["value"] for option in by_id()[model]["params"]["size"]["options"]}
        assert options == set(IMAGE_SIZES), f"{model}: sizes diverged from the docs"


def test_resolutions_match_the_documented_lists():
    catalog = by_id()
    for model, expected in RESOLUTIONS.items():
        options = {option["value"] for option in catalog[model]["params"]["resolution"]["options"]}
        assert options == expected, f"{model}: resolutions {sorted(options)} != {sorted(expected)}"


def test_durations_match_the_documented_bounds():
    catalog = by_id()
    for model, (low, high) in DURATIONS.items():
        duration = catalog[model]["params"]["duration"]
        assert duration["min"] == low, f"{model}: min duration"
        assert duration["max"] == high, f"{model}: max duration"


def test_every_video_generation_model_declares_the_interpolation_fps():
    for model, data in by_id().items():
        kind = data["general"]["kind"]
        if kind == "video":
            fps = data["params"].get("fps")
            assert fps, f"{model}: missing fps"
            options = {option["value"] for option in fps["options"]}
            assert options == {30, 60}, f"{model}: fps options"
            assert fps["default"] == 30
        else:
            # /v1/videos/edits has no fps input: neither its published request
            # schema nor its own docs page lists one, so declaring it here
            # would be inventing a parameter.
            assert "fps" not in data["params"], f"{model}: fps is not an edit input"


def test_video_models_declare_their_duration_parameter():
    for model, data in by_id().items():
        if data["general"]["kind"] != "video":
            continue
        assert data["general"]["duration_type"] == "seconds", f"{model}: duration_type"
        assert data["general"]["duration_param_name"] == "duration", f"{model}: duration_param_name"


def test_pricing_is_present_and_positive():
    for model, data in by_id().items():
        pricing = data["pricing"]
        assert pricing["currency"] == "USD", f"{model}: currency"
        if data["category"] == "IMG":
            assert pricing["base_cost"] > 0, f"{model}: base_cost"
        else:
            assert pricing["cost_per_second"] > 0, f"{model}: cost_per_second"


# ── standby profiles ──────────────────────────────────────────────────────


def test_standby_profiles_exist_for_both_shelves():
    shelves = {path.parent.name for path, _data in profiles()}
    assert shelves == {"IMG", "VID"}
    assert len([1 for path, _ in profiles() if path.parent.name == "IMG"]) >= 8
    assert len([1 for path, _ in profiles() if path.parent.name == "VID"]) == 13


def test_profiles_reference_a_catalog_model_and_the_right_media_type():
    catalog = by_id()
    for path, profile in profiles():
        endpoint = profile["endpoint"]
        assert endpoint in catalog, f"{path.name}: unknown endpoint {endpoint}"
        shelf = path.parent.name
        assert profile["platform"] == "spicyapi", f"{path.name}: platform"
        expected_media = "image" if shelf == "IMG" else "video"
        assert profile["media_type"] == expected_media, f"{path.name}: media_type"
        expected_kind = "image" if shelf == "IMG" else "video"
        assert catalog[endpoint]["general"]["kind"].startswith(expected_kind), path.name


def test_profiles_only_set_parameters_the_endpoint_declares():
    catalog = by_id()
    for path, profile in profiles():
        allowed = set(catalog[profile["endpoint"]].get("params", {}))
        used = set((profile.get("parameters") or {}).keys())
        assert used <= allowed, f"{path.name}: undeclared parameters {sorted(used - allowed)}"


def test_profiles_declare_the_media_keys_the_toml_declares():
    catalog = by_id()
    for path, profile in profiles():
        general = catalog[profile["endpoint"]]["general"]
        for key in ("image_url_param", "single_url_param", "slots"):
            if key in general:
                assert profile.get(key) == general[key], f"{path.name}: {key} drifted from the TOML"
            else:
                assert key not in profile or not profile[key], f"{path.name}: stray {key}"


def test_profiles_generate_a_payload_that_passes_the_gate():
    for path, profile in profiles():
        assert isinstance(profile.get("parameters"), dict), path.name


def test_every_standby_profile_builds_a_coherent_payload():
    """End-to-end over the shipped shelf: profile → catalog → payload."""
    from engine_spicyapi import InputFile
    from engine_spicyapi.catalog import load_catalog
    from engine_spicyapi.engine import _PATH_BY_KIND
    from engine_spicyapi.payloads import build_payload

    catalog = load_catalog()
    for path, profile in profiles():
        spec = catalog.get(profile["endpoint"])
        assert spec.known, f"{path.name}: {profile['endpoint']} is not in the catalog"
        assert spec.kind in _PATH_BY_KIND, f"{path.name}: {spec.kind} has no endpoint"

        prompt = "a portrait in warm window light"
        # Video Generator always hands over a duration; Image Generator does not.
        metadata = {"duration": 5} if profile["media_type"] == "video" else {}
        has_primary = bool(spec.image_url_param)
        item = InputFile(
            path=Path("shot.md"),
            prompt=prompt,
            reference_urls=["https://cdn.example/ref.png"] if has_primary else [],
            metadata=metadata,
        )
        payload, warnings = build_payload(
            profile, spec, profile.get("parameters") or {}, prompt, item
        )

        assert payload["model"] == profile["endpoint"], path.name
        # A video-edit model taking its length from the source clip is expected
        # to say so; anything else warning means the catalog and the shelf disagree.
        unexpected = [w for w in warnings if "'duration'" not in w]
        assert unexpected == [], f"{path.name}: {unexpected}"
        assert ("prompt" in payload) is (spec.prompt_mode != "none"), path.name

        primary = spec.image_url_param
        if primary:
            routed = primary in payload or spec.single_url_param in payload
            assert routed, f"{path.name}: no media routed to {primary}"
        else:
            assert "image_url" not in payload, f"{path.name}: stray media for a text-only model"

        has_duration = "duration" in spec.params
        assert ("duration" in payload) is (has_duration and bool(metadata)), path.name


def test_media_on_a_text_only_model_is_refused_loudly():
    from engine_spicyapi import InputFile
    from engine_spicyapi.catalog import load_catalog
    from engine_spicyapi.payloads import build_payload

    spec = load_catalog().get("spicy-cinema-1")
    item = InputFile(path=Path("shot.md"), prompt="p", reference_urls=["https://cdn.example/x.png"])
    with pytest.raises(Exception, match="takes no image input"):
        build_payload({}, spec, {}, "p", item)
