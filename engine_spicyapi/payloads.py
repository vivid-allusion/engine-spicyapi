"""Request-input construction — pure functions, no I/O.

Split out of ``engine.py`` so the mapping from (profile, endpoint TOML,
Markdown input) to a SpicyAPI ``input`` object is unit-testable on its own.

SpicyAPI's request body is always the same shape::

    {"model": "<model id>", "input": {<model-specific fields>}}

so this module only has to decide what goes into ``input``:

* the profile's ``parameters``, coerced to the types the TOML declares;
* the prompt, composed from ``prompt_prefix`` + the Markdown line 1 + ``prompt_suffix``;
* media, routed to the model's primary media key and its named slots;
* the Markdown file's duration, when the model has such an input.

A model that does not declare a field rejects the whole request with ``400``,
so an undeclared profile parameter is dropped **with a visible warning** rather
than sent: the provider's message is per field and easy to miss in a batch.
"""

from __future__ import annotations

from typing import Any

from .catalog import NON_PAYLOAD_KEYS, PROMPT_KEY, EndpointSpec
from .datatypes import EngineError, InputFile

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


def build_input(
    profile: dict,
    spec: EndpointSpec,
    params: dict,
    prompt: str,
    item: InputFile,
) -> tuple[dict, list[str]]:
    """Build the ``input`` object for one Markdown file.

    Returns the input and any advisory warnings (never raises for a value the
    provider will reject — the API's message is more precise than ours).
    """
    warnings: list[str] = []
    payload: dict[str, Any] = {}

    for key, value in params.items():
        if key in NON_PAYLOAD_KEYS or key == PROMPT_KEY:
            continue
        if is_unset(value):
            continue
        if spec.known and not spec.declares(key):
            warnings.append(
                f"{spec.model} does not declare '{key}' — dropped "
                f"(it would be rejected with HTTP 400)"
            )
            continue
        payload[key] = encode_param(key, value, spec)

    if prompt and not spec.prompt_never:
        payload[PROMPT_KEY] = prompt

    _add_media(payload, profile, spec, item)
    _add_duration(payload, profile, spec, item, warnings)
    return payload, warnings


def is_unset(value: Any) -> bool:
    """True for a value that means "not set" rather than a real input.

    Profiles ship every declared param, so optional ones arrive as ``""``
    (or ``None``/empty list). Sending those is a 400 — ``negative_prompt: ""``
    is not the same request as omitting it.
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


def _add_media(payload: dict, profile: dict, spec: EndpointSpec, item: InputFile) -> None:
    key = primary_key(profile, spec)
    declared = str(profile.get("image_url_param") or spec.image_url_param or "")
    if item.reference_urls:
        if spec.known and not declared:
            # A catalog model with no primary media key takes no media input at
            # all (text-to-image, text-to-video). Routing the URL to a guessed
            # key would send an input the model rejects, and dropping it would
            # silently discard the user's media — so refuse, loudly.
            raise EngineError(
                f"{spec.model} takes no media input, but {item.path.name} carries "
                f"{len(item.reference_urls)} URL(s) — use a model that edits, "
                f"animates or continues them, or move them to that model's profile."
            )
        payload[key] = shape_media(key, item.reference_urls, spec)

    for slot, urls in (item.references or {}).items():
        if urls:
            payload[slot] = shape_media(slot, urls, spec)
    for slot in spec.required_slots:
        if not payload.get(slot):
            raise EngineError(
                f"{spec.model} requires the '{slot}' input, but {item.path.name} "
                f"does not provide it"
            )


def _add_duration(
    payload: dict, profile: dict, spec: EndpointSpec, item: InputFile, warnings: list[str]
) -> None:
    """Apply the Markdown file's duration, when the model has such an input.

    Generators always hand the Engine a duration (Markdown `duration:`, else
    the profile default), so a model that takes its length from the input clip
    would otherwise be sent an undeclared key and 400. Unknown models keep the
    old behavior: the profile knows best.
    """
    duration = item.metadata.get("duration")
    if duration is None:
        return
    key = str(profile.get("duration_param_name") or spec.duration_param_name or "duration_seconds")
    if spec.known and not spec.declares(key):
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
