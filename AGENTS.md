## Architecture: Engine SDK Wrapper

This repo is an **Engine** in the studiolot ecosystem. It wraps the
**Spicy API** (spicyapi.com) and exposes a uniform interface that Generators
call.

### The layers

```
studiolot (TUI) → Generator (script) → **SpicyAPI Engine** → Provider API
```

Generators (Image Generator, Video Generator) are provider-agnostic. They
discover this Engine, load it via `engine_loader.py`, and call
`engine.run(inputs)`. All Spicy API-specific logic lives here.

### Contract (gold parity with engine-replicate)

- **`Engine.__init__` is PURE.** No network I/O, no SDK import. It stores the
  profile, output_dir, api_key and on_progress callback (plus the parsed
  shipped catalog).
- **`Engine.run(inputs: list[InputFile]) -> list[OutputFile]`** is the ONLY
  entry point Generators call. Returns ALL results — success and failure — as
  OutputFile objects. Never raises for per-Markdown-file failures.
- **Error results carry `expected_path`**: the destination filename is
  precomputed BEFORE the API call (profile `output_format`/media default
  extension) so Generators can write error placeholders at the exact name.
- **`EngineError`** is for unrecoverable pre-flight failures (missing API
  key, invalid profile) and for a local refusal to drop media silently.
- **`datatypes.py`** defines InputFile, OutputFile, ProgressEvent,
  EngineError — the gold interface, verbatim.
- **`metadata.py`** is zero-dependency (stdlib only). studiolot imports this
  (NOT engine.py) to read PROVIDER_NAME for the TUI label.
- **`endpoints/`** TOMLs are the model catalog, read by the Engine at run
  time (see below).
- **`profiles/standby/`** holds publishable YAML profiles by category
  (`IMG/`, `VID/`), seeded into a Generator's `USER-FILES/02.STANDBY/`
  filtered by the Generator's declared media type.

### Why four modules instead of gold's one engine.py

Gold posts one model to one endpoint, so it can hardcode the few provider
quirks. The Spicy API has **no per-model endpoint**: every model id goes to
one of four paths, and which path, which primary media key and whether a
prompt is even accepted differ per model. Keeping those facts in the shipped
TOMLs (one source of truth for the wizard *and* the Engine) is what keeps
adding a model a data change, not a code change.

| Module | Owns |
|---|---|
| `engine.py` | Orchestration: batch loop, retries, circuit breaker, saving, progress |
| `transport.py` | The wire: JSON client, downloads, `ProviderError`, error classification, `Retry-After`, retry policy |
| `payloads.py` | Pure request building: param coercion, media routing, prompt composition |
| `catalog.py` | Endpoint TOML loading; `kind`, prompt mode, primary media key, slot shapes |
| `tasks.py` | Video task polling (async provider surface) |
| `outputs.py` | Downloading artifacts; extension resolution from the bytes that arrived |

## NON-NEGOTIABLE: Endpoint TOMLs mirror the live schema

Every input the model accepts server-side MUST be declared in the TOML — as
the `[general]` primary media key, a `[general] slots` entry, or a
`[params.*]` entry. A missing param is a bug, not an editorial choice; an
invented param is a bug too. Enums, ranges and defaults mirror the docs.

Verified by `scripts/check_schema_sync.py`, which fetches
`https://api.spicyapi.com/openapi.json` (public — no key needed) and runs two
levels of check:

1. **Coverage (per endpoint).** The union of what the catalog declares for a
   kind must contain every property the endpoint accepts.
2. **No invention (per TOML).** No TOML may declare a key its endpoint does
   not accept, except the documented exceptions in `TOML_ONLY`.

Which *individual model* accepts which input is not in the OpenAPI document
(it lives in `GET /v1/models`, behind a key) — it is pinned to the provider's
docs snapshot in `tests/test_endpoints.py`: sizes, resolutions, duration
bounds, prompt mode, primary media key, and which models carry `fps`. Every
standby profile is then built through the real catalog and payload builder, so
a profile that the Engine could not run fails the suite.

```
python scripts/check_schema_sync.py            # live schema — exit 1 = drift, do not merge
python scripts/check_schema_sync.py --schema openapi.json   # offline / pinned
```

One documented exception: `scale` on `spicy-upscale-1` is real (the endpoint
prose documents it) but absent from the published request schema.

**Open question — `fps` on the edit models.** The video-generation reference
says `fps` 30/60 applies "on every video model", but `/v1/videos/edits` has no
`fps` in either its request schema or its own docs page, so the catalog
declares it on generation models only. If a live run proves otherwise, add it
to those three TOMLs and record the reason in `TOML_ONLY`.

## Fail loud, never silently drop media

- Media URLs on a model whose catalog entry declares **no primary media key**
  (pure text-to-image / text-to-video) are refused per file with an
  `EngineError` — routing them to a guessed key would send a rejected input,
  and dropping them would discard the user's media.
- More URLs than a single-URL slot accepts is an `EngineError` (gold parity).
- A Markdown duration handed to a model with no `duration` input is dropped
  **with a visible warning** (the output keeps the source length).
- `fps` outside 30/60 — the Generator's internal 24 fps — is dropped with a
  warning; it is not a provider value.

### Provider details

| Field | Value |
|-------|-------|
| **Platform** | `spicyapi` |
| **API key env var** | `SPICYAPI_API_KEY` |
| **Key pattern** | `^sk-spicy-` |
| **Homepage** | https://www.spicyapi.com |
| **Base URL** | `https://api.spicyapi.com` |
| **Auth** | `Authorization: Bearer sk-spicy-…` |
| **Docs snapshot** | `../engine-spicyapi.ai-docs` (sibling repo, `llm-full.txt`) |
| **API style** | REST. Images are synchronous (`POST /v1/images/generations`, `/v1/images/edits`); video is an async task (`POST /v1/videos/generations` or `/v1/videos/edits` → poll `GET /v1/videos/tasks/{id}`). |
| **Runtime deps** | none — stdlib `urllib` only |

Errors: `{"error": {"message", "type"}}`; 401/402/403 abort the batch
(auth/balance), 422 is a per-file moderation decline that costs nothing and
carries `error.moderation_id` (appeal via `POST /v1/moderations/appeals`),
429/503 retry (honouring `Retry-After`).

### Scope (v1)

Covered: image generation and editing (7 models) and video generation and
editing (13 models) — the two categories a Generator consumes, matching
gold's populated catalog (`replicate`'s TXT/Vision dirs hold only
`.gitkeep`; so do these).

Not covered yet: chat / role-play, speech, transcription, embeddings, live
voice calls, and character management. They are plain REST additions
(`/v1/chat/completions`, `/v1/audio/speech`, …) and would land as a `TXT`
catalog plus a chat branch in `_dispatch`.

### Engine discovery

- Repo directory: `engine-spicyapi`, Python package: `engine_spicyapi`
- `engine_loader.py` handles the hyphen→underscore mapping
- Discovery order: project `ENGINES/` clone first, pip-installed package as
  fallback. Fleet registration lives in `f-capacitor`
  (`hhc/engine_registry.CORE_ENGINE_PLATFORMS`) — see the README's "Fleet
  registration" note; that list is the only place a new engine name is added.

### Source File Map

| File | Purpose |
|------|---------|
| `engine_spicyapi/engine.py` | Engine implementation — batch loop, retries, circuit breaker |
| `engine_spicyapi/transport.py` | HTTP client, `ProviderError`, error classification, retry policy |
| `engine_spicyapi/payloads.py` | Pure request-body construction |
| `engine_spicyapi/catalog.py` | Endpoint TOML loading (spec for the payload builder) |
| `engine_spicyapi/tasks.py` | Video task polling |
| `engine_spicyapi/outputs.py` | Artifact downloads + extension sniffing |
| `engine_spicyapi/datatypes.py` | InputFile, OutputFile, ProgressEvent, EngineError |
| `engine_spicyapi/metadata.py` | Zero-dependency identity constants |
| `engine_spicyapi/__init__.py` | Re-exports + `list_standby_profiles(media_type)` |
| `engine_spicyapi/endpoints/` | TOML catalog (IMG/VID populated; TXT/Vision reserved) |
| `engine_spicyapi/profiles/standby/` | Publishable YAML profiles by category |
| `scripts/check_schema_sync.py` | Live schema ↔ TOML drift gate |
| `tests/test_engine.py` | Orchestration tests (fully mocked transport) |
| `tests/test_payloads.py` | Request-building tests |
| `tests/test_catalog.py` | Catalog loader + fallback tests |
| `tests/test_endpoints.py` | Catalog integrity + per-model doc facts + standby profiles |
| `tests/test_outputs.py` | Artifact saving + video task polling |
| `tests/test_schema_sync.py` | Gate logic + pinned-schema end-to-end run |

## Session History

- 2026-10-17 — Created engine-spicyapi: gold-parity interface, stdlib-only
  REST client, 20-model catalog (7 IMG / 13 VID) generated from the docs
  snapshot and the live OpenAPI document, 25 standby profiles across both
  shelves, a two-level live-schema drift gate, and 124 tests (transport fully
  faked). Judgement calls recorded above: no `fps` on the edit models, no
  primary media key on the text-only models, `scale` as the one documented
  schema exception.
