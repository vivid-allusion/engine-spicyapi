"""The live-schema drift gate — logic proven against pinned fixtures.

The fixtures under ``tests/fixtures/models`` are condensed copies of the
provider's public per-model responses (the fields the gate reads). The gate is
run offline here so the suite never depends on the network; the same gate is
run live by ``scripts/check_schema_sync.py`` and by CI.
"""

import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "models"
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("check_schema_sync", ROOT / "scripts" / "check_schema_sync.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def load_pair(name: str):
    """Return (category, path, toml dict, provider doc) for a shipped TOML."""
    path = ROOT / "engine_spicyapi" / "endpoints" / "IMG-Models" / name
    if not path.is_file():
        path = ROOT / "engine_spicyapi" / "endpoints" / "VID-Models" / name
    with open(path, "rb") as handle:
        toml = tomllib.load(handle)
    category = toml["category"]
    doc = gate.load_model(toml["endpoint"], FIXTURES)
    return category, path, toml, doc


def test_gate_passes_offline_on_the_shipped_catalog(capsys):
    assert gate.main(["--models-dir", str(FIXTURES), "--skip-openapi"]) == 0
    assert "0 drifted" in capsys.readouterr().out


def test_gate_helpers():
    assert gate.is_media("image_url", {"x-ui": {"widget": "upload"}}) is True
    assert gate.is_media("image_urls", {"x-ui": {"widget": "multi-upload"}}) is True
    assert gate.is_media("prompt", {"type": "string"}) is False
    assert gate.is_media("reference_image_urls", {"type": "array", "items": {"type": "string"}}) is True
    assert gate.is_media("loras", {"type": "array", "items": {"type": "object"}}) is False

    assert gate.expected_type({"type": "string", "enum": ["a"]}) == "select"
    assert gate.expected_type({"type": "string"}) == "text"
    assert gate.expected_type({"type": "integer"}) == "integer"
    assert gate.expected_type({"type": "number"}) == "number"
    assert gate.expected_type({"type": "boolean"}) == "boolean"

    assert gate.option_value(True) == "true"
    assert gate.option_value("720p") == "720p"


def test_clean_toml_has_no_errors():
    category, path, toml, doc = load_pair("wan-3-0-image-to-video.toml")
    assert gate.check_toml(category, path, toml, doc) == []


def test_a_wrong_enum_is_drift():
    category, path, toml, doc = load_pair("wan-3-0-image-to-video.toml")
    toml["params"]["resolution"]["options"] = [{"value": "8k", "label": "8k"}]
    errors = gate.check_toml(category, path, toml, doc)
    assert any("options" in error for error in errors)


def test_a_wrong_default_is_drift():
    category, path, toml, doc = load_pair("wan-3-0-image-to-video.toml")
    toml["params"]["resolution"]["default"] = "1080p"
    errors = gate.check_toml(category, path, toml, doc)
    assert any("default" in error for error in errors)


def test_a_missing_param_is_drift():
    category, path, toml, doc = load_pair("wan-3-0-image-to-video.toml")
    del toml["params"]["seed"]
    errors = gate.check_toml(category, path, toml, doc)
    assert any("seed" in error and "not declared" in error for error in errors)


def test_an_invented_param_is_drift():
    category, path, toml, doc = load_pair("wan-3-0-image-to-video.toml")
    toml["params"]["fps"] = {"label": "FPS", "type": "integer"}
    errors = gate.check_toml(category, path, toml, doc)
    assert any("fps" in error for error in errors)


def test_a_missing_media_slot_is_drift():
    category, path, toml, doc = load_pair("wan-3-0-image-to-video.toml")
    toml["general"]["slots"] = []
    errors = gate.check_toml(category, path, toml, doc)
    assert any("media inputs not declared" in error for error in errors)


def test_a_wrong_media_lists_shape_is_drift():
    category, path, toml, doc = load_pair("seedream-5-0-pro-image-to-image.toml")
    toml["general"]["media_lists"] = []
    errors = gate.check_toml(category, path, toml, doc)
    assert any("media_lists" in error for error in errors)


def test_a_wrong_prompt_mode_is_drift():
    category, path, toml, doc = load_pair("seedream-5-0-pro-text-to-image.toml")
    toml["general"]["prompt"] = "optional"
    errors = gate.check_toml(category, path, toml, doc)
    assert any("prompt" in error for error in errors)


def test_a_wrong_id_is_drift():
    category, path, toml, doc = load_pair("wan-3-0-image-to-video.toml")
    toml["id"] = "not-the-page-slug"
    errors = gate.check_toml(category, path, toml, doc)
    assert any("pageSlug" in error for error in errors)


def test_a_wrong_kind_is_drift():
    category, path, toml, doc = load_pair("wan-3-0-image-to-video.toml")
    toml["general"]["kind"] = "image"
    errors = gate.check_toml(category, path, toml, doc)
    assert any("kind" in error for error in errors)


def test_a_stale_unrenderable_comment_is_drift():
    category, path, toml, doc = load_pair("wan-2-7-pro-text-to-image.toml")
    text = path.read_text().replace("# UNRENDERABLE: color_palette", "# something else")
    path2 = path.parent / "copy-of-test.toml"
    path2.write_text(text)
    try:
        errors = gate.check_toml(category, path2, toml, doc)
        assert any("UNRENDERABLE" in error for error in errors)
    finally:
        path2.unlink()


OPENAPI_OK = """
openapi: 3.1.0
paths:
  /api/v1/jobs/createTask:
    post:
      operationId: createTask
  /api/v1/jobs/recordInfo:
    get:
      operationId: recordInfo
  /api/v1/jobs/quote:
    post:
      operationId: quoteTask
components:
  schemas:
    CreateTaskRequest:
      required: [model, input]
"""


def test_openapi_contract_check_passes(monkeypatch):
    monkeypatch.setattr(gate, "fetch_text", lambda url: OPENAPI_OK)
    assert gate.check_openapi() == []


def test_openapi_contract_check_catches_a_missing_operation(monkeypatch):
    monkeypatch.setattr(gate, "fetch_text", lambda url: OPENAPI_OK.replace("operationId: createTask", "operationId: gone"))
    errors = gate.check_openapi()
    assert any("createTask" in error for error in errors)


def test_openapi_contract_check_catches_a_changed_request_schema(monkeypatch):
    monkeypatch.setattr(gate, "fetch_text", lambda url: OPENAPI_OK.replace("required: [model, input]", "required: [model]"))
    errors = gate.check_openapi()
    assert any("model+input" in error for error in errors)


def test_read_tomls_finds_every_shipped_toml():
    tomls = gate.read_tomls()
    assert len(tomls) == 24
    assert {category for category, _, _ in tomls} == {"IMG", "VID"}
