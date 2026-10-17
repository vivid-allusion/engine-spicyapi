"""Request-payload construction — pure functions, no I/O.

Split out of ``engine.py`` so the mapping from (profile, endpoint TOML,
Markdown input) to a Spicy API request body is unit-testable on its own.
Mirrors gold's ``_build_replicate_input`` with the Spicy API's extra shapes:

* four request bodies (image generation / image edit / video / video edit)
  selected by the catalog's ``kind``;
* ``fps`` means *frame-interpolate the finished clip* (30 or 60) — the
  generator's internal ``fps`` (typically 24) is not a provider value and is
  dropped with a warning rather than sent;
* dependent parameters are pruned (:mod:`engine_spicyapi.catalog` documents
  which), so a profile carrying ``enhance_mode`` without
  ``enhance_prompt: true`` is not a hard 400.
"""

from __future__ import annotations

from typing import Any

from .catalog import NON_PAYLOAD_KEYS, EndpointSpec
from .datatypes import EngineError, InputFile

FPS_VALUES = (30, 60)
_TRUE = {"true", "yes", "1", "on"}
_FALSE = {"false", "no", "0", "off"}


def compose_prompt(profile: dict, item: InputFile) -> str:
    """``prompt_prefix`` + Markdown prompt + ``prompt_suffix`` (gold order)."""
    prefix = profile.get("prompt_prefix", "") or ""
    suffix = profile.get("prompt_suffix", "") or ""
    return f"{prefix}{item.prompt}{suffix}".strip()


def primary_key(profile: dict, spec: EndpointSpec) -> str:
    """Primary media input key.

    Precedence: profile ``image_url_param`` (the Generator writes it from the
    TOML general block) → the catalog's ``image_url_param`` → ``image_url``.
    """
    for value in (profile.get("image_url_param"), spec.image_url_param):
        if value:
            return str(value)
    return "image_url"


def build_payload(
    profile: dict,
    spec: EndpointSpec,
    params: dict,
    prompt: str,
    item: InputFile,
) -> tuple[dict, list[str]]:
    """Build the request body for one Markdown file.

    Returns the payload and any advisory warnings (never raises for a value
    the provider will reject — the API's message is more precise than ours).
    """
    warnings: list[str] = []
    payload: dict[str, Any] = {}

    for key, value in params.items():
        if key in NON_PAYLOAD_KEYS:
            continue
        if is_unset(value):
            continue
        if key == "fps":
            fps = _fps_value(value)
            if fps is None:
                warnings.append(f"fps {value!r} is not a Spicy API value (30 or 60) — dropped")
                continue
            payload["fps"] = fps
            continue
        payload[key] = encode_param(key, value, spec)

    _prune_dependent(payload)

    payload["model"] = spec.model
    if prompt and not spec.prompt_never:
        payload["prompt"] = prompt

    _add_media(payload, profile, spec, item)
    _add_duration(payload, profile, spec, item, warnings)
    return payload, warnings


def is_unset(value: Any) -> bool:
    """True for a value that means "not set" rather than a real input.

    Profiles ship every declared param, so optional ones arrive as ``""``
    (or ``None``/empty list). Sending those is a 400 — ``action: ""`` is not
    the same request as omitting ``action``.
    """
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, dict, tuple)) and not value:
        return True
    return False


def encode_param(key: str, value: Any, spec: EndpointSpec) -> Any:
    """Coerce one scalar parameter to the type its TOML declares."""
    if spec.is_list_param(key) and isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
        return [part for part in items if part]
    param_type = spec.param_type(key)
    if param_type == "integer":
        return _to_int(value)
    if param_type == "number":
        return _to_float(value)
    if param_type == "boolean":
        return _to_bool(value)
    if param_type == "text" and not isinstance(value, str):
        return str(value)
    return value


def _add_media(
    payload: dict, profile: dict, spec: EndpointSpec, item: InputFile
) -> None:
    key = primary_key(profile, spec)
    declared = str(profile.get("image_url_param") or spec.image_url_param or "")
    if item.reference_urls:
        if spec.known and not declared:
            # A catalog model with no primary media key takes no image input at
            # all (text-to-video, text-to-image). Routing the URL to a guessed
            # key would send an input the model rejects, and dropping it would
            # silently discard the user's media — so refuse, loudly.
            raise EngineError(
                f"{spec.model} takes no image input, but {item.path.name} carries "
                f"{len(item.reference_urls)} URL(s) — use a model that edits, "
                f"animates or continues them, or move them to that model's profile."
            )
        target = key
        if spec.single_url_param and len(item.reference_urls) == 1:
            target = spec.single_url_param
        payload[target] = shape_media(target, item.reference_urls, spec)
    for slot, urls in (item.references or {}).items():
        if urls or slot in spec.required_slots:
            payload[slot] = shape_media(slot, urls, spec)


def _add_duration(
    payload: dict, profile: dict, spec: EndpointSpec, item: InputFile, warnings: list[str]
) -> None:
    """Apply the Markdown file's duration, when the model has such an input.

    Generators always hand the Engine a duration (Markdown `duration:`, else
    the profile default), so an edit model that takes its length from the
    input clip would otherwise be sent an undeclared key and 400. Unknown
    models keep the old behavior: the profile knows best.
    """
    duration = item.metadata.get("duration")
    if duration is None:
        return
    key = str(profile.get("duration_param_name") or spec.duration_param_name or "duration")
    if spec.known and key not in spec.params:
        warnings.append(
            f"{spec.model} takes no '{key}' input — the output keeps the source length"
        )
        return
    payload[key] = encode_param(key, duration, spec)


def shape_media(key: str, urls: list[str], spec: EndpointSpec) -> str | list[str]:
    """Shape URLs for one media parameter.

    Array-typed keys take the full list. Single-URL keys take one URL and
    refuse to silently drop the rest — the provider can only accept one, so
    the media must never be discarded quietly.
    """
    if not urls:
        return []
    if spec.array_media(key):
        return list(urls)
    if len(urls) > 1:
        raise EngineError(
            f"Param '{key}' accepts a single media URL but {len(urls)} were "
            f"provided — fix the markdown: {urls}"
        )
    return urls[0]


def _prune_dependent(payload: dict) -> None:
    if not payload.get("enhance_prompt"):
        payload.pop("enhance_mode", None)
    if not payload.get("character"):
        payload.pop("views", None)


def _fps_value(value: Any) -> int | None:
    try:
        fps = int(value)
    except (TypeError, ValueError):
        return None
    return fps if fps in FPS_VALUES else None


def _to_int(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return value


def _to_float(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return value


def _to_bool(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in _TRUE:
        return True
    if token in _FALSE:
        return False
    return value
