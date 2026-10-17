#!/usr/bin/env python3
"""Live-schema ↔ TOML drift gate for engine-spicyapi.

Gold's gate (engine-replicate) fetches one OpenAPI document per model. The
Spicy API publishes **one** request schema per *endpoint* (four of them:
images/generations, images/edits, videos/generations, videos/edits) and each
endpoint serves several models, so the check runs at two levels:

1. **Coverage (per endpoint).** The union of everything the catalog declares
   for a kind must contain every property the endpoint accepts. A server-side
   property no model declares is a bug — that is the 2026-09-05 p-video
   incident class (users could not express an input the server took).
2. **No invention (per TOML).** No TOML may declare a key the endpoint does
   not accept, except the documented exceptions below.

Which *individual model* accepts which key is a per-model fact the OpenAPI
document does not carry (that lives in ``GET /v1/models`` behind an API key,
and in the provider's docs snapshot, `engine-spicyapi.ai-docs`); it is
asserted by ``tests/test_endpoints.py``.

Usage:
    python scripts/check_schema_sync.py                  # fetch the live schema
    python scripts/check_schema_sync.py --schema FILE     # offline / pinned
    python scripts/check_schema_sync.py --json            # machine-readable

Exit code 1 = drift = do not merge.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

SCHEMA_URL = "https://api.spicyapi.com/openapi.json"
ENDPOINTS_DIR = Path(__file__).resolve().parent.parent / "engine_spicyapi" / "endpoints"

#: kind → (OpenAPI path, HTTP method)
ENDPOINT_BY_KIND = {
    "image": ("/v1/images/generations", "post"),
    "image-edit": ("/v1/images/edits", "post"),
    "video": ("/v1/videos/generations", "post"),
    "video-edit": ("/v1/videos/edits", "post"),
}

#: Set by the Engine, never declared as a TOML param.
ENGINE_MANAGED = frozenset({"model", "prompt"})

#: Declared on purpose although the provider's request schema omits them,
#: because the endpoint's own documentation says the server accepts them.
TOML_ONLY = {
    "scale": (
        "spicy-upscale-1: the endpoint prose documents `scale` 2 or 4, but the "
        "property is missing from the published request schema"
    ),
}


def fetch_schema(url: str = SCHEMA_URL) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def endpoint_properties(schema: dict, path: str, method: str = "post") -> set[str]:
    """Request-body property names for one endpoint."""
    operation = schema.get("paths", {}).get(path, {}).get(method, {})
    content = operation.get("requestBody", {}).get("content", {})
    body = content.get("application/json", {}).get("schema", {})
    return set(body.get("properties", {}) or {})


def load_tomls(endpoints_dir: Path = ENDPOINTS_DIR) -> list[tuple[Path, dict]]:
    """Every endpoint TOML with its parsed content (unparsable files dropped)."""
    loaded = []
    for toml_file in sorted(endpoints_dir.glob("*/*.toml")):
        try:
            with open(toml_file, "rb") as handle:
                loaded.append((toml_file, tomllib.load(handle)))
        except Exception as exc:  # noqa: BLE001 - report, never crash the gate
            print(f"  ! unreadable: {toml_file.name}: {exc}", file=sys.stderr)
    return loaded


def declared_keys(data: dict) -> set[str]:
    """Everything one TOML says the model accepts, media keys included."""
    general = data.get("general") or {}
    keys = set((data.get("params") or {}).keys())
    keys.update(str(slot) for slot in general.get("slots") or ())
    keys.update(str(slot) for slot in general.get("required_slots") or ())
    for key in ("image_url_param", "single_url_param"):
        if general.get(key):
            keys.add(str(general[key]))
    return keys


def kind_of(data: dict) -> str:
    general = data.get("general") or {}
    return str(general.get("kind") or "")


def diff(declared: set[str], server: set[str], allowed_extra: set[str]) -> tuple[set, set]:
    """(missing, extra) — pure, offline-testable."""
    return server - declared - ENGINE_MANAGED, declared - server - ENGINE_MANAGED - allowed_extra


def check(schema: dict, tomls: list[tuple[Path, dict]]) -> list[str]:
    """Return a list of drift findings (empty = clean)."""
    problems: list[str] = []
    by_kind: dict[str, set[str]] = {kind: set() for kind in ENDPOINT_BY_KIND}
    per_kind_files: dict[str, list[str]] = {kind: [] for kind in ENDPOINT_BY_KIND}

    for path, data in tomls:
        kind = kind_of(data)
        if kind not in ENDPOINT_BY_KIND:
            problems.append(f"{path.name}: unknown or missing [general] kind {kind!r}")
            continue
        server = endpoint_properties(schema, *ENDPOINT_BY_KIND[kind])
        declared = declared_keys(data)
        by_kind[kind] |= declared
        per_kind_files[kind].append(path.name)
        missing, extra = diff(declared, server, set(TOML_ONLY))
        del missing  # per-TOML omissions are covered by the per-kind union check below
        if extra:
            problems.append(
                f"{path.name}: declares {sorted(extra)} — not in {ENDPOINT_BY_KIND[kind][0]}"
            )

    for kind, (server_path, _method) in ENDPOINT_BY_KIND.items():
        if not per_kind_files[kind]:
            continue
        server = endpoint_properties(schema, *ENDPOINT_BY_KIND[kind])
        uncovered = server - by_kind[kind] - ENGINE_MANAGED
        if uncovered:
            problems.append(
                f"{server_path}: {sorted(uncovered)} accepted by the server but declared "
                f"by no {kind} TOML"
            )
        print(
            f"  {kind:<11} {server_path:<26} "
            f"{len(by_kind[kind])} declared / {len(server)} server props "
            f"({len(per_kind_files[kind])} TOML)"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", type=Path, help="local openapi.json (offline mode)")
    parser.add_argument("--url", default=SCHEMA_URL, help="schema URL to fetch")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--endpoints-dir", type=Path, default=ENDPOINTS_DIR)
    args = parser.parse_args(argv)

    if args.schema:
        schema = json.loads(Path(args.schema).read_text())
        source = str(args.schema)
    else:
        schema = fetch_schema(args.url)
        source = args.url

    tomls = load_tomls(args.endpoints_dir)
    if not tomls:
        print(f"no endpoint TOMLs under {args.endpoints_dir}", file=sys.stderr)
        return 1

    print(f"schema: {source}   catalog: {len(tomls)} TOML")
    problems = check(schema, tomls)

    if args.json:
        print(json.dumps({"tomls": len(tomls), "problems": problems}, indent=2))
    if problems:
        print(f"\n{len(problems)} drift problem(s) — do not merge:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"OK — {len(tomls)} TOML mirror the live schema, 0 drifted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
