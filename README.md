# engine-spicyapi

Engine wrapper for the [Spicy API](https://www.spicyapi.com) — an
uncensored image, image-editing, video and video-editing provider, billed per
generation from a prepaid balance.

Part of the studiolot Generator / Engine architecture. This repo wraps the
provider's REST API behind a uniform interface that Generators (Image
Generator, Video Generator) call. A Generator never talks to the provider
directly — it calls this Engine.

Reference implementation (the gold standard this repo mirrors):
`github.com/vivid-allusion/engine-replicate`. Full contract:
`docs/architecture/ENGINE_CONTRACT.md` in the studiolot repo.

## Quick start

```bash
git clone https://github.com/vivid-allusion/engine-spicyapi.git
cd engine-spicyapi
# no dependencies: the Engine talks plain REST with the standard library
python -m pytest        # tests need pytest + pyyaml
```

## Usage

```python
from pathlib import Path
from engine_spicyapi import Engine, InputFile

profile = {
    "platform": "spicyapi",
    "media_type": "image",
    "endpoint": "spicy-image-1",
    "parameters": {"size": "1024*1024", "n": 1},
}

engine = Engine(profile=profile, output_dir="/tmp/out", api_key="sk-spicy-...")
inputs = [InputFile(path=Path("markdown-001.md"), prompt="a portrait in warm window light")]
for result in engine.run(inputs):
    print(result.status, result.path or result.error_msg)
```

Video profiles are asynchronous under the hood and polled for you:

```python
profile = {
    "platform": "spicyapi",
    "media_type": "video",
    "endpoint": "spicy-motion-3",
    "image_url_param": "image_url",
    "duration_param_name": "duration",
    "slots": ["last_frame_url", "reference_image_urls", "audio_urls"],
    "parameters": {"resolution": "720P", "duration": 5},
}
```

## API key

```bash
export SPICYAPI_API_KEY=sk-spicy-...
```

Or pass `api_key=` to the constructor, or put it in the project's `.env`
(Generators load that file). Keys are created in the provider's dashboard.

## Two provider rules the Engine enforces

1. **Inputs must be your own outputs.** Edits, video and motion transfer
   accept only URLs this account generated; uploads and third-party URLs are
   rejected by the provider before billing. Markdown files therefore carry
   CDN URLs produced by earlier runs.
2. **Nothing is uploaded from disk.** Every media input is an https URL (or a
   data URL for a couple of inputs), which is why the Engine can stay
   stdlib-only and never handles multipart uploads.

## Model catalog

`endpoints/` holds one TOML per model:

- `IMG-Models/` — 7 models: `spicy-image-1`, `spicy-image-1-pro`,
  `spicy-image-photo-1`, `spicy-image-action-1`, `spicy-image-reference-1`,
  `spicy-image-edit-1`, `spicy-upscale-1`
- `VID-Models/` — 13 models: `spicy-motion-3`, `spicy-motion-3-fast`,
  `spicy-motion-draft-1`, `spicy-motion-2`, `spicy-motion-1`,
  `spicy-cinema-1`, `spicy-cinema-1-image`, `spicy-cinema-1-character`,
  `spicy-video-1`, `spicy-character-video-1`, `spicy-video-edit-1`,
  `spicy-cinema-1-edit`, `spicy-animate-1`
- `TXT-Models/`, `Vision-Models/` — reserved (chat, speech, transcription and
  embeddings are not wrapped yet)

Each TOML declares every input the model accepts, its enum values and
ranges, so the TUI wizard can build the menus and the Engine can build the
body. That claim is enforced:

```bash
python scripts/check_schema_sync.py      # exit 1 = drift = do not merge
```

The gate compares the catalog against the provider's live
`openapi.json` (public, no key needed) at two levels: every server-side
property is declared somewhere, and no TOML declares a key its endpoint does
not accept.

## Repo structure

```
engine-spicyapi/
├── engine_spicyapi/
│   ├── __init__.py           ← re-exports + list_standby_profiles(media_type)
│   ├── engine.py             ← Engine class: batch loop, retries, circuit breaker
│   ├── transport.py          ← HTTP client, ProviderError, error classification, retries
│   ├── payloads.py           ← pure request-body construction
│   ├── catalog.py            ← endpoint TOML loading
│   ├── tasks.py              ← video task polling
│   ├── outputs.py            ← artifact downloads + extension sniffing
│   ├── datatypes.py          ← InputFile, OutputFile, ProgressEvent, EngineError
│   ├── metadata.py           ← zero-dependency constants (studiolot reads this)
│   ├── endpoints/            ← TOML model catalog
│   └── profiles/standby/     ← publishable YAML profiles (IMG/, VID/)
├── scripts/check_schema_sync.py
├── tests/
├── requirements.txt          ← empty: no third-party runtime deps
└── pyproject.toml
```

## Fleet registration

Engines are discovered from a clone directory (`engine-<platform>/`) by
`engine_loader.py`; no registration is needed for a project that clones this
repo. To make `spicyapi` a first-class choice in the F-Capacitor wizard it
must be added to the fleet list — `hhc/engine_registry.CORE_ENGINE_PLATFORMS`
in `f-capacitor` — and to the Generators' supported-platform lists. That is a
one-line-per-repo change tracked separately from this engine.

## Status

Snapshot-free: built against the provider's live OpenAPI document and its
docs snapshot (`engine-spicyapi.ai-docs`). Endpoint TOMLs are the catalog of
record; run the gate before any TOML change or model version bump.
