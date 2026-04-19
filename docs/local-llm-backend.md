# Local LLM Backend — Gemma 4 26B MoE via llama.cpp

A locally-hosted, OpenAI-compatible LLM endpoint that can serve as the model backend for the Simon gov-agency agent during development and offline testing. Runs entirely on the dev workstation's RTX 5070 Ti — no API keys, no cost per request, no network egress.

Set up on 2026-04-19. Lives at `/home/edgardo/llm-lab/` (separate from this repo).

## TL;DR — how to use it

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer local" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemma-4-26b",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

OpenAI SDK:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="local")
resp = client.chat.completions.create(
    model="gemma-4-26b",
    messages=[{"role": "user", "content": "..."}],
)
```

```typescript
import OpenAI from "openai";
const client = new OpenAI({ baseURL: "http://127.0.0.1:8000/v1", apiKey: "local" });
```

Supports: chat completions, streaming, tool/function calling (needs `--jinja` on the server — already enabled).

## What's running

| | |
|---|---|
| Model | Google Gemma 4 26B A4B (MoE, ~4B active per token) |
| Quant | Unsloth Dynamic `UD-IQ2_XXS` GGUF (9.2 GiB, ~2-bit) |
| Backend | `llama.cpp` built from source for Blackwell `sm_120a` |
| GPU | RTX 5070 Ti, 16 GB VRAM (11.3 GB used, 4.6 GB free) |
| Context | 32,768 tokens, KV cache at `q8_0` |
| Throughput | **~170 tok/s** predict, ~1000 tok/s prompt |
| Endpoint | `http://127.0.0.1:8000/v1/*` (OpenAI-compatible) |
| Auth | `Authorization: Bearer local` |
| Model alias | `gemma-4-26b` |

## Quality profile (measured 2026-04-19)

Based on direct benchmarks on this machine — see also `~/llm-lab/slod-bench/` for sample outputs.

**Good at:**
- **Creative writing / long-form prose** — ~5,000 words in 36s, voice transfer was strong on a chat-format novel chapter (character voice, formatting rules, italic monologue conventions all preserved).
- Conversational Q&A / chat.
- Trivial code refactors where the edit is local and unambiguous.
- Tool-call generation — `finish_reason: "tool_calls"` works cleanly, function JSON is valid.

**Bad at (at UD-IQ2_XXS specifically — likely a quant ceiling):**
- **Autonomous file-edit agent loops** (tested via Aider): failed to converge on a "write a unit test" task after 222s of reasoning; silently drifted into *unrelated* files with unsolicited edits (changed a Postgres `ts_rank_cd` normalization flag in a file it wasn't asked about). **Do not trust it with unattended multi-file edits on production code.**
- Long multi-step reasoning chains.
- Precise numerical or symbolic manipulation.

**Verdict for Simon:** suitable for scaffolding prompts, dev-loop conversation, interactive demo sessions, and prose-heavy assistant outputs (briefings, summaries, letter drafts — the kind of thing a gov agent will actually produce). **Not** suitable as the autonomous write-to-disk agent. If Simon's loop includes tool-calling to backend services (read-only lookups, form generation, etc.), that path is fine.

## Upgrade path (if quality is insufficient)

Same model, better quant — all fit in 16 GB VRAM:

| Quant | File size | Notes |
|---|---|---|
| `UD-IQ2_XXS` | 9.88 GB | **Currently running.** 2-bit, fastest, lowest quality |
| `UD-IQ3_XXS` | 11.2 GB | 3-bit, noticeable quality bump |
| `UD-IQ4_XS` | 13.4 GB | **Recommended next step** if UD-IQ2 isn't good enough. 4-bit, ~45 min to swap |
| `UD-Q4_K_XL` | 17.1 GB | Requires CPU offload — will be much slower |

Source: https://huggingface.co/unsloth/gemma-4-26B-A4B-it-GGUF

Alternative models that also fit:
- **Qwen2.5-Coder-14B-Instruct** (Q4/Q5) — known stronger than Gemma 4 MoE on agentic code tasks.
- **Qwen3 32B AWQ** — stronger reasoning, slower; 14–16 GB.
- **Gemma 4 31B Dense** — better quality than 26B MoE on benches, but won't fit; needs offload.

## Operational commands

```bash
# Check server is alive
curl -s http://127.0.0.1:8000/v1/models -H "Authorization: Bearer local"

# Stop server
kill $(cat ~/llm-lab/llamacpp.pid)

# Start server
~/llm-lab/llama.cpp/build/bin/llama-server \
  --model ~/llm-lab/models/gemma-4-26B-A4B-UD-IQ2_XXS/gemma-4-26B-A4B-it-UD-IQ2_XXS.gguf \
  --host 127.0.0.1 --port 8000 \
  --n-gpu-layers 999 --ctx-size 32768 \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --parallel 1 --cont-batching \
  --api-key local --jinja --alias gemma-4-26b \
  > ~/llm-lab/llamacpp.log 2>&1 &
echo $! > ~/llm-lab/llamacpp.pid

# Tail logs (tok/s, prompt stats)
tail -f ~/llm-lab/llamacpp.log
```

## Integration notes for Simon

- **Concurrency:** server is started with `--parallel 1`. If Simon wants parallel requests (e.g. agent-of-agents), bump to `--parallel 2` or `--parallel 4` — expect per-request speed to drop roughly linearly.
- **Streaming:** supported via OpenAI SDK's `stream=True`. llama.cpp emits OpenAI-compatible SSE chunks.
- **Tool calling:** works, but the agent loop framework must accept `finish_reason: "tool_calls"` and honor Gemma 4's chat template (the `--jinja` flag on the server handles template rendering server-side, so any OpenAI-SDK-shaped tool call request works).
- **Context:** 32K is enough for most single-turn agent steps. If Simon needs 128K+, the model technically supports it, but VRAM will not — would need to drop quant further or spill KV to CPU.
- **Model switching:** if Simon needs a different model, the recommended move is a **second llama-server instance on port 8001** rather than swapping in-place, so the dev-loop Gemma endpoint stays stable.
- **Offline guarantee:** after initial model download, the server has zero outbound network dependencies — useful for air-gapped gov demos.

## Known issues / caveats

- **2-bit quant edit drift** — see "Bad at" section above. If Simon uses this for code edits, wrap with strict diff review.
- **First-request latency** — first call after launch is slow (model graph warmup). Second call onward hits ~170 tok/s.
- **Blackwell-only build** — the llama.cpp binary is compiled for `sm_120a`. It won't run on non-Blackwell GPUs without a rebuild.
- **VRAM ceiling** — this box holds 16 GB. Running Simon's other GPU workloads simultaneously will OOM.

## Related

- Unsloth GGUF repo: https://huggingface.co/unsloth/gemma-4-26B-A4B-it-GGUF
- llama.cpp: https://github.com/ggml-org/llama.cpp
- Gemma 4 announcement: https://blog.google/innovation-and-ai/technology/developers-tools/gemma-4/
- Benchmark artifacts on this machine: `~/llm-lab/slod-bench/` (a 5K-word novel chapter generated as a style/quality probe)
