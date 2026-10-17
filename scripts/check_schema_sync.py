#!/usr/bin/env python3
"""Live-schema drift gate: the endpoint TOMLs must mirror the provider's schema.

SpicyAPI publishes, with no API key required:

* ``https://api.spicyapi.ai/console/v1/catalog/models/<model>?locale=en`` —
  one model, including its live ``inputSchema`` (JSON Schema 2020-12), its
  ``paramNotes``, its price tiers and whether it is ``live`` and ``callable``;
* ``https://docs.spicyapi.ai/openapi.yaml`` — the OpenAPI 3.1 contract.

This gate re-derives every fact a TOML claims and fails on any disagreement:

1. **Endpoint** — the model exists, is ``live`` and ``callable``, its modality
   matches the TOML's category, and ``id`` is the provider's own ``pageSlug``.
2. **Coverage** — every property the model accepts is declared: the prompt by
   ``[general] prompt``, media by ``image_url_param`` / ``slots``, everything
   else by a ``[params.*]`` table. Properties whose ``x-ui.widget`` is one the
   studiolot wizard has no input for (``object-list``, ``json``,
   ``chat-messages``) are exempt, and *exactly* that set is exempt.
3. **No invention** — no TOML may declare a key the model does not accept.
4. **Shape** — each declared param's type, ``options`` (the enum), ``default``,
   ``min``/``max`` and ``hidden`` match the schema; ``media_lists`` names
   exactly the media keys the schema declares as arrays.
5. **Wire** — the two operations the Engine calls exist in the OpenAPI
   contract, and ``CreateTaskRequest`` still requires ``model`` and ``input``.

```
python scripts/check_schema_sync.py                      # live — exit 1 = drift
python scripts/check_schema_sync.py --models-dir saved/  # offline / pinned
python scripts/check_schema_sync.py --skip-openapi       # model catalog only
```
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - the gate needs PyYAML
    yaml = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parent.parent
ENDPOINTS_DIR = ROOT / "engine_spicyapi" / "endpoints"
MODEL_URL = "https://api.spicyapi.ai/console/v1/catalog/models/{model}?locale=en"
OPENAPI_URL = "https://docs.spicyapi.ai/openapi.yaml"

CATEGORY_MODALITY = {"IMG": "image", "VID": "video", "TXT": "text", "VISION": "video"}
CATEGORY_DIRS = {"IMG": "IMG-Models", "VID": "VID-Models", "TXT": "TXT-Models", "VISION": "Vision-Models"}

#: x-ui.widget values the studiolot wizard has no input for. A property that
#: carries one is exempt from coverage; the exemption is itself checked.
UNRENDERABLE_WIDGETS = {"object-list", "json", "chat-messages"}
MEDIA_WIDGETS = {"upload", "multi-upload"}

TIMEOUT = 30.0
USER_AGENT = "engine-spicyapi-check-schema-sync/0.1 (+https://github.com/vivid-allusion/engine-spicyapi)"


def is_media(key: str, prop: dict) -> bool:
    ui = prop.get("x-ui") or {}
    if ui.get("widget") in MEDIA_WIDGETS:
        return True
    return (
        prop.get("type") == "array"
        and key.endswith("_urls")
        and (prop.get("items") or {}).get("type") == "string"
    )


def expected_type(prop: dict) -> str:
    ptype = prop.get("type")
    if ptype == "boolean":
        return "boolean"
    if ptype == "integer":
        return "integer"
    if ptype == "number":
        return "number"
    if ptype == "string" and prop.get("enum"):
        return "select"
    return "text"


def option_value(value) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url, headers={"Accept": "text/yaml", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read().decode("utf-8")


def load_model(model: str, models_dir: Path | None) -> dict:
    if models_dir is not None:
        path = models_dir / (model.replace("/", "__") + ".json")
        with open(path) as handle:
            return json.load(handle)
    return fetch_json(MODEL_URL.format(model=urllib.parse.quote(model, safe="")))


def read_tomls() -> list[tuple[str, Path, dict]]:
    out: list[tuple[str, Path, dict]] = []
    for category, dir_name in CATEGORY_DIRS.items():
        directory = ENDPOINTS_DIR / dir_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.toml")):
            with open(path, "rb") as handle:
                out.append((category, path, tomllib.load(handle)))
    return out


def check_toml(category: str, path: Path, toml: dict, doc: dict) -> list[str]:
    errors: list[str] = []
    where = f"{path.parent.name}/{path.name}"

    def fail(message: str) -> None:
        errors.append(f"{where}: {message}")

    data = doc.get("data") or {}
    schema = data.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    media = {key for key, prop in props.items() if is_media(key, prop)}
    unrenderable = {
        key for key, prop in props.items() if (prop.get("x-ui") or {}).get("widget") in UNRENDERABLE_WIDGETS
    }
    params = toml.get("params") or {}
    general = toml.get("general") or {}

    model = str(toml.get("endpoint") or "")
    if not model:
        fail("no 'endpoint'")
        return errors
    if data.get("model") != model:
        fail(f"endpoint {model!r} is not the model the provider returns ({data.get('model')!r})")
    if toml.get("id") != data.get("pageSlug"):
        fail(f"id {toml.get('id')!r} is not the provider's pageSlug ({data.get('pageSlug')!r})")
    if data.get("status") != "live":
        fail(f"model status is {data.get('status')!r}, not 'live'")
    if not data.get("callable"):
        fail("model is not callable")
    if data.get("modality") != CATEGORY_MODALITY[category]:
        fail(f"category {category} implies {CATEGORY_MODALITY[category]}, model says {data.get('modality')!r}")
    if general.get("kind") != data.get("modality"):
        fail(f"[general] kind {general.get('kind')!r} != modality {data.get('modality')!r}")

    # prompt
    if "prompt" in required:
        want_prompt = "required"
    elif "prompt" in props:
        want_prompt = "optional"
    else:
        want_prompt = "none"
    if general.get("prompt") != want_prompt:
        fail(f"[general] prompt is {general.get('prompt')!r}, schema says {want_prompt!r}")

    # media routing
    primary = str(general.get("image_url_param") or "")
    slots = [str(s) for s in general.get("slots") or ()]
    declared = ({primary} if primary else set()) | set(slots)
    if len(slots) != len(set(slots)):
        fail("duplicate keys in [general] slots")
    if primary and primary not in media:
        fail(f"image_url_param {primary!r} is not a media input of this model")
    for slot in slots:
        if slot not in media:
            fail(f"slot {slot!r} is not a media input of this model")
    if primary in slots:
        fail(f"{primary!r} is both image_url_param and a slot")
    if declared != media:
        missing = sorted(media - declared)
        extra = sorted(declared - media)
        if missing:
            fail(f"media inputs not declared: {missing}")
        if extra:
            fail(f"declared media keys the model does not accept: {extra}")

    # array shapes
    arrays = sorted(key for key in media if props[key].get("type") == "array")
    lists = sorted(str(s) for s in general.get("media_lists") or ())
    if lists != arrays:
        fail(f"media_lists {lists} != the schema's array media keys {arrays}")

    required_slots = [str(s) for s in general.get("required_slots") or ()]
    for slot in required_slots:
        if slot not in slots:
            fail(f"required_slots names {slot!r}, which is not a slot")
        elif slot not in required:
            fail(f"required_slots names {slot!r}, but the schema does not require it")

    # coverage
    for key in props:
        if key == "prompt" or key in media or key in unrenderable:
            continue
        if key not in params:
            fail(f"schema property {key!r} is not declared in [params.*]")

    # no invention
    for key in params:
        if key not in props:
            fail(f"[params.{key}] is not a property of this model")
        elif key in media:
            fail(f"[params.{key}] duplicates a media input — declare it in [general]")

    # exact unrenderable set
    declared_unrenderable = {
        line.split(":", 1)[1].strip().split(" ")[0]
        for line in path.read_text().splitlines()
        if line.strip().startswith("# UNRENDERABLE:")
    }
    if declared_unrenderable != unrenderable:
        fail(
            f"UNRENDERABLE comment set {sorted(declared_unrenderable)} != "
            f"the schema's unrenderable properties {sorted(unrenderable)}"
        )

    # per-param shape
    for key, raw in params.items():
        prop = props.get(key)
        if not isinstance(prop, dict) or not isinstance(raw, dict):
            continue
        want = expected_type(prop)
        if raw.get("type") != want:
            fail(f"[params.{key}] type is {raw.get('type')!r}, schema implies {want!r}")
        enum = prop.get("enum")
        if enum is not None:
            want_options = [option_value(value) for value in enum]
            got_options = [option_value(option.get("value")) for option in raw.get("options") or []]
            if got_options != want_options:
                fail(f"[params.{key}] options {got_options} != enum {want_options}")
        elif raw.get("options"):
            fail(f"[params.{key}] declares options but the schema has no enum")
        if "default" in prop:
            if raw.get("default") != prop["default"]:
                fail(f"[params.{key}] default is {raw.get('default')!r}, schema says {prop['default']!r}")
        elif "default" in raw:
            fail(f"[params.{key}] declares a default but the schema has none")
        for schema_key, toml_key in (("minimum", "min"), ("maximum", "max")):
            if schema_key in prop:
                if raw.get(toml_key) != prop[schema_key]:
                    fail(
                        f"[params.{key}] {toml_key} is {raw.get(toml_key)!r}, "
                        f"schema says {prop[schema_key]!r}"
                    )
            elif toml_key in raw:
                fail(f"[params.{key}] declares {toml_key} but the schema has no {schema_key}")
        want_hidden = bool((prop.get("x-ui") or {}).get("advanced"))
        if bool(raw.get("hidden", False)) != want_hidden:
            fail(f"[params.{key}] hidden is {bool(raw.get('hidden', False))}, schema says {want_hidden}")
    return errors


def check_openapi() -> list[str]:
    if yaml is None:
        return ["PyYAML is not installed — cannot read the OpenAPI contract"]
    document = yaml.safe_load(fetch_text(OPENAPI_URL))
    errors: list[str] = []
    paths = document.get("paths") or {}
    for path, method, operation in (
        ("/api/v1/jobs/createTask", "post", "createTask"),
        ("/api/v1/jobs/recordInfo", "get", "recordInfo"),
        ("/api/v1/jobs/quote", "post", "quoteTask"),
    ):
        node = paths.get(path) or {}
        entry = node.get(method) or {}
        if not entry:
            errors.append(f"OpenAPI: {method.upper()} {path} is gone")
        elif entry.get("operationId") != operation:
            errors.append(
                f"OpenAPI: {method.upper()} {path} operationId is {entry.get('operationId')!r}, "
                f"expected {operation!r}"
            )
    request = ((document.get("components") or {}).get("schemas") or {}).get("CreateTaskRequest") or {}
    required = set(request.get("required") or [])
    if not {"model", "input"} <= required:
        errors.append(f"OpenAPI: CreateTaskRequest no longer requires model+input (has {sorted(required)})")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models-dir", type=Path, default=None, help="offline per-model JSON directory")
    parser.add_argument("--skip-openapi", action="store_true", help="check the model catalog only")
    args = parser.parse_args(argv)

    tomls = read_tomls()
    if not tomls:
        print("No endpoint TOMLs found — nothing to check.", file=sys.stderr)
        return 1

    errors: list[str] = []
    models = 0
    for category, path, toml in tomls:
        model = str(toml.get("endpoint") or "")
        try:
            doc = load_model(model, args.models_dir)
        except Exception as exc:  # noqa: BLE001 - reported as drift
            errors.append(f"{path.name}: cannot load {model!r}: {exc}")
            continue
        models += 1
        errors.extend(check_toml(category, path, toml, doc))

    if not args.skip_openapi:
        try:
            errors.extend(check_openapi())
        except Exception as exc:  # noqa: BLE001 - reported as drift
            errors.append(f"cannot load the OpenAPI contract: {exc}")

    if errors:
        print(f"SCHEMA DRIFT — {len(errors)} problem(s):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    source = str(args.models_dir) if args.models_dir else "the live catalog"
    print(f"{models} TOML mirror the live schema ({source}), 0 drifted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
