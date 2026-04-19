# SIM-002 — `llamacpp` provider branch silently drops `--api-key`

**Severity:** Medium (P2) — functional, not security. Forces a workaround for all local-LLM deployments.
**Discovered:** 2026-04-19 while wiring Simon to a local llama.cpp server.
**Reporter:** edgardo
**Component:** `crates/api/src/registry.rs`

## Summary

In `provider_from_config`, the `llamacpp` / `llama-cpp` / `llama-server` branch never calls `.with_api_key(cli.api_key)`. Any API key provided via `--api-key`, `SIMON_API_KEY`, or the `api_key` config field is silently dropped, so Simon talks to the configured `--api-base` with no `Authorization: Bearer ...` header.

## Reproduction

```
simon --provider llamacpp \
      --api-base http://host.docker.internal:8000/v1 \
      --api-key local \
      -p "hello"
```

Observed: llama.cpp server (started with `--api-key local`) responds 401. Expected: 200 with `Authorization: Bearer local` attached.

## Root cause

Around `crates/api/src/registry.rs:144`, the `llamacpp` arm builds the provider without the `.with_api_key()` call that every other branch uses. Looks like an omission when the branch was copied from `ollama` (which also doesn't use keys).

## Fix

```rust
// in registry.rs, llamacpp branch:
let mut provider = openai_compat_providers::llama_cpp(api_base);
if let Some(key) = cli.api_key.clone().or_else(|| config.api_key.clone()) {
    provider = provider.with_api_key(key);
}
Box::new(provider)
```

Same pattern as the `openai`, `custom_openai`, and `google` branches. Also add the same fallback chain those use: CLI flag → env var → config file.

## Test

Integration test `crates/api/tests/llamacpp_auth.rs`:
- Start a mock OpenAI server that asserts `Authorization` header is present.
- Invoke Simon headless with `--provider llamacpp --api-key testkey`.
- Assert the mock received the header with `Bearer testkey`.

## Workaround currently in use

`/home/edgardo/projectsd/simon/scripts/bridge_llamacpp.py` — a Python stdlib TCP proxy on `172.17.0.1:8000` that injects `Authorization: Bearer local` server-side. Delete this script once SIM-002 lands.

## Acceptance criteria

- `simon --provider llamacpp --api-key X` sends `Authorization: Bearer X`.
- `SIMON_API_KEY=X simon --provider llamacpp` does the same.
- Integration test added and passes in CI.
- `scripts/bridge_llamacpp.py` and the `docker-compose.override.yml` references to it can be deleted; doc `docs/local-llm-backend.md` updated to drop the bridge step.

## Related

- SIM-001 (shell ACL bypass) — discovered in the same session; higher severity; unrelated fix.
