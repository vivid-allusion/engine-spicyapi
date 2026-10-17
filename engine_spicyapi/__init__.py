from pathlib import Path

from .engine import Engine
from .datatypes import InputFile, OutputFile, ProgressEvent, EngineError

_STANDBY_DIR = Path(__file__).parent / "profiles" / "standby"


def list_standby_profiles(media_type: str | None = None) -> list[Path]:
    """Return sorted standby YAML paths, optionally filtered by media type.

    Standby profiles live in category subdirectories mirroring the endpoint
    catalog: ``profiles/standby/IMG/`` and ``profiles/standby/VID/``. The
    loading Generator declares its media type (Image Generator → IMG,
    Video Generator → VID) and the engine seeds only the matching shelf.

    Args:
        media_type: ``'IMG'`` or ``'VID'`` (case-insensitive). ``None``
            returns every standby profile — legacy behavior.

    Returns:
        Sorted list of standby YAML paths.
    """
    if not _STANDBY_DIR.is_dir():
        return []
    if media_type:
        source = _STANDBY_DIR / media_type.upper()
        if not source.is_dir():
            return []
        return sorted(source.glob("*.yaml"))
    files = sorted(_STANDBY_DIR.glob("*.yaml"))
    for sub in sorted(_STANDBY_DIR.iterdir()):
        if sub.is_dir():
            files.extend(sorted(sub.glob("*.yaml")))
    return files


__all__ = [
    "Engine",
    "InputFile",
    "OutputFile",
    "ProgressEvent",
    "EngineError",
    "list_standby_profiles",
]
