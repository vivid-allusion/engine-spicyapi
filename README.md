# engine-spicyapi

Engine wrapper for **[SpicyAPI](https://spicyapi.ai)** — the model aggregator at
`https://api.spicyapi.ai` (image, video, audio and text models from Alibaba,
ByteDance, Kling, MiniMax, Google, OpenAI and others, behind one key).

Part of the [studiolot](https://github.com/vivid-allusion/studiolot) Generator /
Engine architecture. Generators (Image Generator, Video Generator) never talk
to a provider directly — they call this Engine, which wraps SpicyAPI's
asynchronous task API behind one uniform interface.

> **Not `spicyapi.com`.** "Spicy API" by Wayfinity Ltd is a different,
> unrelated service that happens to share the name. This Engine wraps
> `spicyapi.ai` only.

## Quick start

```bash
git clone https://github.com/vivid-allusion/engine-spicyapi.git
cd engine-spicyapi
export SPICY_API_KEY=sk-spicy-...
```

No runtime dependencies: the Engine talks to the provider with `urllib` from
the standard library.

## Usage

```python
from pathlib import Path

from engine_spicyapi import Engine, InputFile

profile = {
    "platform": "spicyapi",
    "media_type": "video",
    "endpoint": "alibaba/wan-3.0/image-to-video",
    "image_url_param": "image_url",
    "slots": ["last_image_url"],
    "parameters": {"resolution": "720p", "duration_seconds": 5},
    "prompt_prefix": "",
    "prompt_suffix": "",
}

engine = Engine(profile=profile, output_dir="/tmp/out", api_key=None)

inputs = [
    InputFile(
        path=Path("markdown-001.md"),
        prompt="slow push-in, light rain, neon reflections on wet stone",
        reference_urls=["https://cdn.example/first-frame.png"],
    ),
]

for result in engine.run(inputs):
    print(result.status, result.path or result.error_msg)
```

Every generation is a task: the Engine submits `POST /jobs/createTask`, polls
`GET /jobs/recordInfo` until the state leaves `{queued, running}`, then
downloads `output.assets[].url`. Each submission carries an `Idempotency-Key`,
so a retried call returns the original task instead of holding funds twice.

## The catalog

`engine_spicyapi/endpoints/` holds one TOML per model — 12 image and 12 video
endpoints, the wizard's model list and the Engine's routing table. The
`endpoint` field is the provider's own model id, so a profile is portable:

| Shelf | Endpoints |
|---|---|
| `IMG-Models/` | `bytedance/seedream-5.0-pro/{text-to-image,edit}`, `google/nano-banana-2.1/{text-to-image,edit}`, `openai/gpt-image-2.5-sunburst/{text-to-image,edit}`, `alibaba/qwen-image-3.0/{text-to-image,edit}`, `alibaba/z-image-turbo/text-to-image`, `alibaba/wan-2.7-pro/text-to-image`, `spicyapi/image-upscaler-v1/upscale`, `spicyapi/background-remover-v1/edit` |
| `VID-Models/` | `alibaba/wan-3.0/{text-to-video,image-to-video,reference-to-video,video-extend}`, `bytedance/seedance-2.5/{text-to-video,image-to-video}`, `bytedance/seedance-2.0-mini/image-to-video`, `minimax/h3/{text-to-video,image-to-video}`, `kling/3.0/text-to-video`, `vidu/q3-turbo/image-to-video`, `spicyapi/video-upscaler-v1/upscale` |

The catalog is **generated from and gated against** the provider's public
per-model endpoint, which needs no API key:

```bash
python scripts/check_schema_sync.py          # live — exit 1 means drift
```

## Standby profiles

`engine_spicyapi/profiles/standby/{IMG,VID}/` holds publishable YAML profiles,
seeded into a Generator's `USER-FILES/02.STANDBY/` filtered by its media type.

## Fleet registration

The engine is discovered from the directory name (`engine-spicyapi` →
platform `spicyapi`). The only place a new engine's name is registered is
`hhc/engine_registry.CORE_ENGINE_PLATFORMS` in
[f-capacitor](https://github.com/vivid-allusion/f-capacitor); the API key
variable is `SPICY_API_KEY` (`hhc/constants.PROVIDER_ENV_VAR`).

## Documentation snapshot

Provider docs live in the sibling repo
[`engine-spicyapi.ai-docs`](https://github.com/vivid-allusion/engine-spicyapi.ai-docs)
(`llm-full.txt` + the OpenAPI contract).

## License

MIT.
