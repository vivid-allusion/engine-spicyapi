"""The endpoint TOML catalog, read by the Engine at run time.

Gold (engine-replicate) keeps its catalog for the TUI wizard and hardcodes the
few provider quirks the Engine needs. SpicyAPI (spicyapi.ai) is different
again: it is a **model aggregator** with one generic asynchronous task API —
``POST /jobs/createTask`` with ``{model, input}`` — so every model id is posted
to the same path, but *which* fields ``input`` may carry, which of them are
media, and whether a prompt is accepted are all per model.

Those facts live in the shipped TOMLs, one file per model, so adding a model is
a data change and never a code change. The Engine reads its own catalog at run
time; the same files are the wizard's model list.

The catalog is generated from — and gated against — the provider's **public**
per-model endpoint
``https://api.spicyapi.ai/console/v1/catalog/models/<model>?locale=en``, which
returns the model's live JSON Schema. See ``scripts/check_schema_sync.py``.

Stdlib only. A missing or broken TOML is skipped, never raised: the Engine
falls back to an inferred spec so a hand-written profile still runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

ENDPOINTS_DIR = Path(__file__).parent / "endpoints"

CATEGORY_DIRS = {
    "IMG": "IMG-Models",
    "VID": "VID-Models",
    "TXT": "TXT-Models",
    "VISION": "Vision-Models",
}

#: Profile keys that are generator plumbing, never provider inputs.
NON_PAYLOAD_KEYS = frozenset({"prompt_prefix", "prompt_suffix", "kind", "api_path"})

#: The provider's prompt field, on every model that takes one.
PROMPT_KEY = "prompt"


@dataclass
class EndpointSpec:
    """Everything the payload builder needs to know about one model."""

    id: str
    model: str
    category: str = ""
    kind: str = "video"
    label: str = ""
    image_url_param: str = ""
    slots: tuple[str, ...] = ()
    required_slots: tuple[str, ...] = ()
    media_lists: tuple[str, ...] = ()
    duration_param_name: str = "duration_seconds"
    prompt_mode: str = "required"
    known: bool = True
    params: dict = field(default_factory=dict)

    @property
    def prompt_required(self) -> bool:
        return self.prompt_mode == "required"

    @property
    def prompt_never(self) -> bool:
        return self.prompt_mode == "none"

    def array_media(self, key: str) -> bool:
        """True when the endpoint declares this media key as an array."""
        return key in self.media_lists

    def param_type(self, key: str) -> str:
        param = self.params.get(key)
        if isinstance(param, dict):
            return str(param.get("type") or "")
        return ""

    def is_list_param(self, key: str) -> bool:
        """True when a *scalar* param is declared as a comma-separated list."""
        param = self.params.get(key)
        return bool(isinstance(param, dict) and param.get("list"))

    def declares(self, key: str) -> bool:
        """True when the TOML declares this key anywhere the Engine reads.

        Params come from ``[params.*]``, the prompt from ``[general] prompt``,
        and media from ``[general] image_url_param`` / ``slots``.
        """
        return (
            key in self.params
            or key == PROMPT_KEY
            or key == self.image_url_param
            or key in self.slots
        )


def infer_kind(model: str) -> str:
    """Best-effort kind for a model id the catalog does not carry."""
    mid = (model or "").lower()
    if any(token in mid for token in ("image", "upscale", "background", "erase")):
        return "image"
    return "video"


def fallback_spec(model: str, category: str = "") -> EndpointSpec:
    """A spec for a model the shipped catalog does not know.

    Unknown models keep the profile's word: nothing is dropped and no key is
    guessed, so a hand-written profile that knows the schema still runs.
    """
    return EndpointSpec(
        id=model,
        model=model,
        category=category,
        kind=infer_kind(model),
        label=model,
        image_url_param="image_url",
        prompt_mode="optional",
        known=False,
    )


class Catalog:
    """Model id → :class:`EndpointSpec`, loaded once from the TOMLs."""

    def __init__(self, specs: dict[str, EndpointSpec] | None = None):
        self._specs = specs or {}

    def __len__(self) -> int:
        return len(self._specs)

    def get(self, model: str) -> EndpointSpec:
        """Spec for ``model`` — the shipped catalog, else an inferred one."""
        spec = self._specs.get(model)
        if spec is not None:
            return spec
        return fallback_spec(model)

    def models(self) -> list[str]:
        return sorted(self._specs)


def load_catalog(endpoints_dir: Path | None = None) -> Catalog:
    """Read every endpoint TOML under ``endpoints/``. Never raises."""
    if tomllib is None:
        return Catalog()
    root = Path(endpoints_dir) if endpoints_dir else ENDPOINTS_DIR
    specs: dict[str, EndpointSpec] = {}
    for category, dir_name in CATEGORY_DIRS.items():
        directory = root / dir_name
        if not directory.is_dir():
            continue
        for toml_file in sorted(directory.glob("*.toml")):
            spec = _read_spec(toml_file, category)
            if spec is not None and spec.model:
                specs.setdefault(spec.model, spec)
    return Catalog(specs)


def _read_spec(path: Path, category: str) -> EndpointSpec | None:
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("id"):
        return None
    general = data.get("general") if isinstance(data.get("general"), dict) else {}
    model = str(data.get("endpoint") or data["id"])
    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    return EndpointSpec(
        id=str(data["id"]),
        model=model,
        category=str(data.get("category") or category),
        kind=str(general.get("kind") or infer_kind(model)),
        label=str(data.get("label") or data["id"]),
        image_url_param=str(general.get("image_url_param") or ""),
        slots=tuple(str(s) for s in general.get("slots") or ()),
        required_slots=tuple(str(s) for s in general.get("required_slots") or ()),
        media_lists=tuple(str(s) for s in general.get("media_lists") or ()),
        duration_param_name=str(general.get("duration_param_name") or "duration_seconds"),
        prompt_mode=str(general.get("prompt") or "required"),
        params=params,
    )
