"""Saving generated artifacts — extension resolution and downloads.

Split out of ``engine.py`` so the batch loop stays about control flow. The
extension rules matter here: the Spicy API returns *durable CDN URLs* whose
container the provider chooses, so the file a run writes is named from the
bytes that actually arrived (magic number), never from a guess.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .transport import Client

DEFAULT_EXTENSIONS = {"image": ".png", "video": ".mp4"}

_MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)


def clean_format(value) -> str:
    """A profile ``output_format`` reduced to a safe extension word ('' if odd)."""
    text = str(value or "").strip().lstrip(".").lower()
    return text if text and all(char.isalnum() for char in text) else ""


def default_extension(profile: dict, media_type: str) -> str:
    """Extension for precomputed names (the error placeholders' ``expected_path``)."""
    output_format = clean_format((profile.get("parameters") or {}).get("output_format"))
    if output_format:
        return f".{output_format}"
    return DEFAULT_EXTENSIONS.get(media_type, ".png")


def sniff_extension(data: bytes, media_type: str) -> str:
    """Real extension from the stream's magic bytes."""
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:8] == b"ftyp":
        return ".mp4"
    return DEFAULT_EXTENSIONS.get(media_type, ".png")


def save_results(
    client: Client,
    dest_dir: Path,
    urls: list[str],
    media_type: str,
    stem: str,
    idx: int,
    on_download: Callable[[], None] | None = None,
    on_saved: Callable[[Path], None] | None = None,
) -> list[Path]:
    """Download every URL into ``dest_dir``; returns the files written.

    Multiple outputs from one Markdown file get ``_0``, ``_1`` … suffixes so a
    six-image call never collides with itself.
    """
    if not urls:
        return []
    dest_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%y%m%d_%H%M%S")
    saved: list[Path] = []
    for index, url in enumerate(urls):
        if on_download:
            on_download()
        data = client.download(url)
        suffix = "" if len(urls) == 1 else f"_{index}"
        dest = dest_dir / f"{timestamp}-{stem}-{idx}{suffix}{sniff_extension(data, media_type)}"
        with open(dest, "wb") as handle:
            handle.write(data)
        if on_saved:
            on_saved(dest)
        saved.append(dest)
    return saved
