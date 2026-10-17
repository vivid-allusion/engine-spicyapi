"""The endpoint TOML catalog, read by the Engine at run time.

Gold (engine-replicate) keeps its catalog for the TUI wizard and hardcodes
the few provider quirks the Engine needs. The Spicy API has no per-model
endpoint: every model id is posted to one of four paths, and which path,
which primary media key and whether a prompt is accepted differ per model.
So the Engine reads its own shipped TOMLs — one source of truth for both the
wizard and the payload builder, so adding a model adds no code.

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

#: Media keys the Spicy API declares as arrays. Used only when a TOML does
#: not spell the shape out; the catalog always wins.
ARRAY_MEDIA_KEYS = frozenset(
    {
        "image_urls",
        "reference_image_urls",
        "reference_images",
        "reference_videos",
        "reference_audios",
        "audio_urls",
    }
)

#: Profile keys that are generator plumbing, never provider inputs.
NON_PAYLOAD_KEYS = frozenset({"prompt_prefix", "prompt_suffix", "kind", "api_path"})


@dataclass
class EndpointSpec:
    """Everything the payload builder needs to know about one model."""

    id: str
    model: str
    category: str = ""
    kind: str = "video"
    label: str = ""
    image_url_param: str = ""
    single_url_param: str = ""
    slots: tuple[str, ...] = ()
    required_slots: tuple[str, ...] = ()
    duration_param_name: str = "duration"
    duration_type: str = ""
    prompt_mode: str = ""
    known: bool = True
    params: dict = field(default_factory=dict)

    @property
    def prompt_required(self) -> bool:
        return self.prompt_mode == "required"

    @property
    def prompt_never(self) -> bool:
        return self.prompt_mode == "none"

    def array_media(self, key: str) -> bool:
        """True when the endpoint declares this media key as an array.

        Name-based on purpose: the Spicy API spells every array media key
        with a trailing ``s`` (``image_urls``, ``reference_image_urls``,
        ``audio_urls``), and single-URL keys (``image_url``,
        ``last_frame_url``, ``video_url``, ``audio_url``, ``mask_url``) never
        collide with that. ``[general] slots`` lists the *declared* media
        keys — array and single alike — so it is not a shape signal.
        """
        return key.endswith("_urls") or key in ARRAY_MEDIA_KEYS

    def param_type(self, key: str) -> str:
        param = self.params.get(key)
        if isinstance(param, dict):
            return str(param.get("type") or "")
        return ""

    def is_list_param(self, key: str) -> bool:
        param = self.params.get(key)
        return bool(isinstance(param, dict) and param.get("list"))


def infer_kind(model: str) -> str:
    """Best-effort kind for a model id the catalog does not carry."""
    mid = (model or "").lower()
    if mid.startswith("spicy-image") or mid.startswith("spicy-upscale"):
        return "image-edit" if ("edit" in mid or mid.startswith("spicy-upscale")) else "image"
    if mid.startswith(
        (
            "spicy-motion",
            "spicy-cinema",
            "spicy-video",
            "spicy-character",
            "spicy-animate",
        )
    ):
        return "video-edit" if ("edit" in mid or mid.startswith("spicy-animate")) else "video"
    return "video"


def infer_prompt_mode(kind: str, model: str) -> str:
    mid = (model or "").lower()
    if any(token in mid for token in ("action", "reference", "upscale", "animate")):
        return "none"
    if kind == "video":
        return "optional"
    return "required"


def fallback_spec(model: str, category: str = "") -> EndpointSpec:
    """A spec for a model the shipped catalog does not know."""
    kind = infer_kind(model)
    return EndpointSpec(
        id=model,
        model=model,
        category=category,
        kind=kind,
        label=model,
        image_url_param="image_url",
        prompt_mode=infer_prompt_mode(kind, model),
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
    kind = str(general.get("kind") or infer_kind(model))
    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    return EndpointSpec(
        id=str(data["id"]),
        model=model,
        category=str(data.get("category") or category),
        kind=kind,
        label=str(data.get("label") or data["id"]),
        image_url_param=str(general.get("image_url_param") or ""),
        single_url_param=str(general.get("single_url_param") or ""),
        slots=tuple(str(s) for s in general.get("slots") or ()),
        required_slots=tuple(str(s) for s in general.get("required_slots") or ()),
        duration_param_name=str(general.get("duration_param_name") or "duration"),
        duration_type=str(general.get("duration_type") or ""),
        prompt_mode=str(general.get("prompt") or infer_prompt_mode(kind, model)),
        params=params,
    )
