## Architecture: Engine SDK Wrapper

This repo is an **Engine** in the studiolot ecosystem. It wraps
**SpicyAPI** (`spicyapi.ai`) — the model aggregator at
`https://api.spicyapi.ai` — and exposes a uniform interface that Generators
call.

Not to be confused with **`spicyapi.com`** ("Spicy API", Wayfinity Ltd), an
unrelated service that shares the name. The docs snapshot repo is named
`engine-spicyapi.ai-docs` for that reason; the `.ai` is the domain.

### The layers

```
studiolot (TUI) → Generator (script) → **SpicyAPI Engine** → Provider API
```

Generators (Image Generator, Video Generator) are provider-agnostic. They
discover this Engine, load it via `engine_loader.py`, and call
`engine.run(inputs)`. All SpicyAPI-specific logic lives here.

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
- **`EngineError`** is for unrecoverable pre-flight failures (missing API key,
  invalid profile) and for a local refusal to drop media silently.
- **`datatypes.py`** defines InputFile, OutputFile, ProgressEvent, EngineError
  — the gold interface, verbatim.
- **`metadata.py`** is zero-dependency (stdlib only). studiolot imports this
  (NOT engine.py) to read PROVIDER_NAME for the TUI label.
- **`endpoints/`** TOMLs are the model catalog, read by the Engine at run time
  (see below).
- **`profiles/standby/`** holds publishable YAML profiles by category
  (`IMG/`, `VID/`), seeded into a Generator's `USER-FILES/02.STANDBY/`
  filtered by the Generator's declared media type.

### Why six modules instead of gold's one engine.py

Gold posts one model to one endpoint, so it can hardcode the few provider
quirks. SpicyAPI is a **model aggregator** with one generic task API —
`POST /jobs/createTask` with `{model, input}` — so the *call* is uniform but
the *input* is not: which fields `input` may carry, which of them are media,
and whether a prompt is accepted are all per model, and a single unknown field
is rejected with HTTP 400. Those facts live in the shipped TOMLs, one source of
truth for both the wizard and the Engine, so adding a model is a data change.

| Module | Owns |
|---|---|
| `engine.py` | Orchestration: batch loop, retries, circuit breaker, saving, progress |
| `transport.py` | The wire: JSON client, downloads, `ProviderError`, error classification, `Retry-After` |
| `payloads.py` | Pure `input` construction: param coercion, media routing, prompt, duration |
| `catalog.py` | Endpoint TOML loading; kind, prompt mode, primary media key, slot shapes |
| `tasks.py` | The async surface: `createTask`, `recordInfo` polling, asset collection |
| `outputs.py` | Downloading artifacts; extension resolution from the bytes that arrived |

## NON-NEGOTIABLE: Endpoint TOMLs mirror the live schema

SpicyAPI publishes, with **no API key required**:

- `https://api.spicyapi.ai/console/v1/catalog/models/<model>?locale=en` — one
  model with its live `inputSchema` (JSON Schema 2020-12), `paramNotes`, price
  tiers and `status`/`callable`;
- `https://docs.spicyapi.ai/openapi.yaml` — the OpenAPI 3.1 contract.

`scripts/check_schema_sync.py` re-derives every fact a TOML claims and fails on
any disagreement:

1. **Endpoint** — the model exists, is `live` and `callable`, its modality
   matches the TOML's category, and `id` is the provider's own `pageSlug`.
2. **Coverage** — every property the model accepts is declared: the prompt by
   `[general] prompt`, media by `image_url_param`/`slots`, everything else by a
   `[params.*]` table.
3. **No invention** — no TOML may declare a key the model does not accept.
4. **Shape** — each param's type, `options` (the enum), `default`, `min`/`max`
   and `hidden` match the schema; `media_lists` names exactly the media keys
   the schema declares as arrays.
5. **Wire** — the operations the Engine calls still exist in the contract and
   `CreateTaskRequest` still requires `model` + `input`.

```
python scripts/check_schema_sync.py                      # live — exit 1 = drift
python scripts/check_schema_sync.py --models-dir tests/fixtures/models --skip-openapi
```

**The one exemption.** A property whose `x-ui.widget` is `object-list`, `json`
or `chat-messages` cannot be rendered by the studiolot wizard, which knows only
select/integer/number/boolean/text. Such properties are skipped and recorded in
the TOML as a `# UNRENDERABLE:` comment; the gate checks that the commented set
is *exactly* the schema's unrenderable set, so the exemption cannot silently
widen. Today it covers two fields: `multi_prompt` (kling/3.0) and
`color_palette` (wan-2.7-pro).

**Integer-valued enums.** A schema enum on an `integer`/`number` property is
declared as `type = "integer"`/`"number"` **with** `options`, because the
Engine must coerce the value to a number. The wizard renders those as a text
field (its `widget_kind` only maps `select` to a radio); the `note` names the
allowed values. This affects `duration_seconds` on the video models, whose
value the Video Generator overrides from the Markdown file anyway.

## Fail loud, never silently drop media

- A profile parameter the model does not declare is dropped **with a visible
  warning** — sending it would fail the whole request with HTTP 400.
- Media URLs on a model whose TOML declares **no primary media key** (pure
  text-to-image / text-to-video) are refused per file with an `EngineError` —
  routing them to a guessed key would send a rejected input, and dropping them
  would discard the user's media.
- More URLs than a single-URL slot accepts is an `EngineError` (gold parity).
- A required slot the Markdown does not provide is an `EngineError`.
- A Markdown duration handed to a model with no `duration_seconds` input is
  dropped **with a visible warning** (the output keeps the source length).
- A task that ends `failed`/`expired` becomes a per-file error result; only
  auth, balance, spend-cap and region failures abort the batch.

### Provider details

| Field | Value |
|-------|-------|
| **Platform** | `spicyapi` |
| **API key env var** | `SPICY_API_KEY` |
| **Key pattern** | `^sk-spicy-` |
| **Homepage** | https://spicyapi.ai |
| **Base URL** | `https://api.spicyapi.ai/api/v1` |
| **Auth** | `Authorization: Bearer sk-spicy-…` |
| **Docs snapshot** | `../engine-spicyapi.ai-docs` (`llm-full.txt`, `openapi.yaml`) |
| **API style** | REST. Every generation is async: `POST /jobs/createTask` → poll `GET /jobs/recordInfo?taskId=…` → `output.assets[].url` (signed, ~20 min). |
| **Runtime deps** | none — stdlib `urllib` only |

Responses use the `{code, msg, data, request_id}` envelope; `data.state` — not
the envelope code — expresses task success. Error codes: `401`/`40201`/`40202`/
`403*` abort the batch; `429`/`500`/`503`/`50301` retry honouring `Retry-After`;
`400`/`40003`/`40004`/`404`/`409`/`40901`/`413` are per file. A `50302`
(streaming failure) is deliberately **not** retried — the provider requires a
new `Idempotency-Key`, so it surfaces per file. Task failures carry a closed
`errorCode` (`content_rejected`, `rate_limited`, `timeout`, …).

### Scope (v1)

Covered: image generation and editing (12 models) and video generation, editing
and extending (12 models) — the two categories a Generator consumes, matching
gold's populated catalog (`replicate`'s TXT/Vision dirs hold only `.gitkeep`;
so do these).

Not covered: chat / text, speech, transcription, audio uploads and webhooks.
They are plain REST additions (`/v1/chat/completions`, `/jobs/stream`, …) and
would land as a `TXT` catalog plus a branch in `_dispatch`. Quote-before-create
(`POST /jobs/quote`) is not wired in either; every profile is expected to carry
its own price display.

### Engine discovery

- Repo directory: `engine-spicyapi`, Python package: `engine_spicyapi`
- `engine_loader.py` handles the hyphen→underscore mapping
- Discovery order: project `ENGINES/` clone first, pip-installed package as
  fallback. Fleet registration lives in `f-capacitor`
  (`hhc/engine_registry.CORE_ENGINE_PLATFORMS`); that list is the only place a
  new engine name is added.

### Source File Map

| File | Purpose |
|------|---------|
| `engine_spicyapi/engine.py` | Engine implementation — batch loop, retries, circuit breaker |
| `engine_spicyapi/transport.py` | HTTP client, `ProviderError`, error classification, retry policy |
| `engine_spicyapi/payloads.py` | Pure `input` construction |
| `engine_spicyapi/catalog.py` | Endpoint TOML loading (spec for the payload builder) |
| `engine_spicyapi/tasks.py` | `createTask` + `recordInfo` polling |
| `engine_spicyapi/outputs.py` | Artifact downloads + extension sniffing |
| `engine_spicyapi/datatypes.py` | InputFile, OutputFile, ProgressEvent, EngineError |
| `engine_spicyapi/metadata.py` | Zero-dependency identity constants |
| `engine_spicyapi/__init__.py` | Re-exports + `list_standby_profiles(media_type)` |
| `engine_spicyapi/endpoints/` | TOML catalog (IMG/VID populated; TXT/Vision reserved) |
| `engine_spicyapi/profiles/standby/` | Publishable YAML profiles by category |
| `scripts/check_schema_sync.py` | Live-schema ↔ TOML drift gate |
| `tests/fixtures/models/` | Condensed public catalog responses, for the offline gate test |
| `tests/test_engine.py` | Orchestration tests (fully faked transport) |
| `tests/test_payloads.py` | Input-building tests |
| `tests/test_catalog.py` | Catalog loader + fallback tests |
| `tests/test_tasks.py` | Task creation, polling and asset collection |
| `tests/test_endpoints.py` | Catalog integrity + standby profiles built for real |
| `tests/test_outputs.py` | Artifact saving + extension sniffing |
| `tests/test_schema_sync.py` | Gate logic, proven offline against the fixtures |

## Session History

- 2026-10-17 — Rebuilt for **spicyapi.ai**. The first cut wrapped
  `spicyapi.com` because the docs snapshot in `engine-spicyapi.ai-docs` held
  that service's `llms-full.txt`; the snapshot was corrected to the `.ai`
  corpus plus the OpenAPI contract, and the Engine was rewritten against the
  aggregator's task API. 24-model catalog generated from the public per-model
  schema, 51 standby profiles, a five-check live gate (no key needed), and 346
  tests. Two latent bugs in the first cut were found and fixed: a success with
  no assets raised `UnboundLocalError`, and an absent required slot was never
  refused.
