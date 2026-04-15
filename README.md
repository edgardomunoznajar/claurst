# Simon

> An AI coding agent, bound by your ACLs.

Simon is a Rust-based terminal coding agent for Australian government agencies. It is a fork of [`claurst`](https://github.com/Kuberwastaken/claurst) (itself a clean-room Rust reimplementation of Claude Code's behaviour) stripped back to a defensible core and wrapped with:

- **An SSO-bound, deterministic ACL gate** in Rust that runs before every read/write tool call. The LLM cannot reach the check to talk it out of a `Deny`.
- **PSPF clearance awareness** — Unofficial / Official / Official:Sensitive / Protected — surfaced from OIDC ID-token claims.
- **A fail-closed audit sink** that commits every decision to disk (fsync per write) before the tool is allowed to act.
- **A single-`docker compose up` demo stack** with Dex (OIDC), a mock NES enterprise-search ACL service, and a real Australian Hansard corpus stamped with synthetic PSPF labels.

This repository is the `simon` branch of a fork of Kuberwastaken/claurst. Commits past `upstream/main` are the Simon-specific work. The upstream project remains the source of the underlying agent.

---

## Quick start (working as of 2026-04-16)

```bash
# 1. Clone the fork and switch to the simon branch
git clone git@github.com:edgardomunoznajar/claurst.git simon
cd simon && git checkout simon

# 2. Drop your Gemini / Anthropic key in .env (git-ignored)
cat > .env <<'EOF'
GEMINI_API_KEY=AIzaSy...
EOF

# 3. Let the browser resolve Dex by its container name
echo '127.0.0.1 dex' | sudo tee -a /etc/hosts

# 4. Build and start the full stack
docker compose up -d --build

# 5. Run the end-to-end smoke test
set -a; source .env; set +a
GOOGLE_API_KEY="$GEMINI_API_KEY" scripts/smoke_test.sh --keep-up
```

A green run looks like this:

```
sarah-unofficial  UNOFFICIAL  list  deny   /workspace/protected
                                           reason: requires PROTECTED clearance; you have UNOFFICIAL
bill-official     OFFICIAL    list  deny   /workspace/protected
                                           reason: requires PROTECTED clearance; you have OFFICIAL
alice-protected   PROTECTED   list  allow  /workspace/protected
alice-protected   PROTECTED   read  allow  /workspace/protected/2022-09-08-anthony-albanese-…
```

Three SSO-bound users, three clearances, the same query, three different enforcement decisions. Every line is fsync'd into `/var/simon/audit.jsonl` inside the container.

---

## Interactive browser-login demo

Two terminals.

**Terminal 1 — audit log tail:**
```bash
docker compose exec simon tail -f /var/simon/audit.jsonl
```

**Terminal 2 — Simon TUI as alice:**
```bash
docker compose exec simon simon --provider google -m gemini-2.5-flash
```

Simon prints the Dex authorisation URL. Open it in your browser, log in as any of:

| Clearance | Email | Password |
|---|---|---|
| UNOFFICIAL | `sarah@simon.demo` | `Password!1` |
| OFFICIAL | `bill@simon.demo` | `Password!1` |
| PROTECTED | `alice@simon.demo` | `Password!1` |

Dex redirects to `http://localhost:8765/callback`, Simon captures the code, exchanges it for tokens, and drops you into the TUI as that principal. Ask Simon to read something from `/workspace/protected/` and watch the gate decision land in terminal 1.

---

## Architecture in one diagram

```
 ┌───────────────────────────────────────────────────────────────────────┐
 │  Your browser ─────── Dex OIDC (localhost:5556 via /etc/hosts)        │
 │                                                                       │
 │  Terminal ───▶  simon container (uid 1000, non-root)                  │
 │                 ├─ simon_login.rs  ◀── OIDC auth-code + PKCE          │
 │                 ├─ ToolContext.acl_gate()  ─▶  AclEnforcer (Rust)     │
 │                 │                              ├─ static_json         │
 │                 │                              └─ nes_http ─▶  NES    │
 │                 ├─ file_read / grep / bash / web_fetch / ...          │
 │                 └─ audit sink (fsync per decision)                    │
 │                                                                       │
 │  /workspace/{unofficial,official,official-sensitive,protected}/       │
 │      ← corpus named volume, seeded from demo/corpus/ by corpus-init   │
 └───────────────────────────────────────────────────────────────────────┘
```

### Three layers of defence

| Layer | Component | Role |
|---|---|---|
| **L3** | `ToolContext::permission_handler` | Existing user-approval dialog for destructive ops (UX). Cannot override an L2 Deny. |
| **L2** | `simon-acl` crate, `ToolContext::acl_gate` | **Deterministic**, **fail-closed**, **auditable** per-call gate. Runs in Rust code the LLM cannot reach. |
| **L1** | Container FS view + egress firewall + seccomp | The real trust boundary. Not fully enforced by this docker-compose — production needs host-level iptables + seccomp profile. See `docs/simon/threat-model.md`. |

The AclEnforcer is necessary but not sufficient. The honest security story is in `docs/simon/threat-model.md`.

---

## Repository layout

```
simon/
├── README.md                          ← you are here
├── docker-compose.yml                 ← the demo stack
├── .env                               ← GEMINI_API_KEY (git-ignored)
│
├── src-rust/                          ← the Rust workspace (simon binary)
│   └── crates/
│       ├── acl/       ← simon-acl: AclEnforcer trait + 3 backends + audit
│       ├── cli/       ← the `simon` binary + simon_login.rs (OIDC)
│       ├── tools/     ← read/write tools, each calling ctx.acl_gate()
│       ├── core/ api/ query/ tui/ commands/ mcp/ bridge/ plugins/
│
├── deploy/                            ← docker-compose service contexts
│   ├── simon/Dockerfile  deploy/simon/acl.json
│   ├── dex/config.yaml
│   ├── nes-mock/app.py  deploy/nes-mock/policy.json
│   └── corpus-init/seed.sh
│
├── demo/corpus/                       ← 60 Hansard speeches with .acl.json sidecars
│   ├── unofficial/          (27 docs)
│   ├── official/            (13 docs)
│   ├── official-sensitive/  (10 docs)
│   └── protected/           (10 docs)
│
├── scripts/
│   ├── prep_hansard.py        ← corpus generator (idempotent, seeded)
│   ├── smoke_test.sh          ← E2E ACL assertion against the stack
│   └── build_presentation.py  ← rebuilds docs/simon/simon-demo.pptx
│
└── docs/simon/
    ├── essential-eight-control-4.md   ← ML3 mapping table (flagship positioning)
    ├── owasp-llm-top-10.md            ← coverage table by LLM risk
    ├── threat-model.md                ← honest limits, known bypasses
    ├── pitch.md                       ← 90-second APS-agency one-pager
    └── simon-demo.pptx                ← 14-slide pitch deck
```

Upstream files we haven't rewritten (`docs/`, `spec/`, `public/`, `index.html`) are from the `claurst` heritage and will be replaced or pruned at some point.

---

## How it actually works

1. **Startup, pre-login.** `cli/src/main.rs` reads `SIMON_ACL_POLICY` (a static-JSON file) or `SIMON_NES_URL` (the mock NES service), constructs an `AclEnforcer` trait object, and stores it in `ToolContext`. Default is fail-closed: untagged files require `Protected`, shell mode is `Off`.
2. **Login.** `simon_login::login()` runs an OIDC auth-code-with-PKCE flow against `SIMON_OIDC_ISSUER`. On success, a `SimonPrincipal` is constructed with `subject`, `email`, `clearance` (derived from the `sub` claim), `session_id`, and `expires_at`. Stored in `ToolContext` alongside the enforcer. If login fails, we fall back to an anonymous `Unofficial` principal which the fail-closed defaults treat as essentially read-nothing.
3. **Tool call.** The LLM decides to call, say, `file_read`. The tool parses the path, calls `ctx.acl_gate(ResourceRef::file(path), Operation::Read)`. This:
   a. Runs `AclEnforcer::check(principal, resource, op)` → `AclDecision`.
   b. Commits an `AuditEvent` to the audit sink. Sink error = tool call fails.
   c. On `Deny`, returns `Err(...)` — the LLM sees "ACL denied: <reason>" as the tool result and reasons about it.
4. **Observe.** Every decision is in `/var/simon/audit.jsonl` inside the container — append-only, one JSON line per decision, fsync'd before return.

### The fail-closed surface

- `static_json.rs` — `default_file_min_clearance: Protected` for unmatched paths
- `shell_mode: Off` — bash is disabled entirely unless explicitly allowlisted; Allowlist mode rejects any command containing pipes, heredocs, backticks, `$(...)`, redirection, `;`, `&&`, `||`
- `nes_client.rs` — any non-2xx or timeout from the NES service returns `Deny`
- `audit.rs::FileJsonlSink::write` — fsync is load-bearing; a sink failure aborts the tool call

---

## Runbook

### Bring up / tear down

```bash
docker compose up -d --build          # first time (~10 min Rust compile)
docker compose up -d                   # subsequent runs
docker compose down                    # stop, keep volumes
docker compose down -v                 # stop and wipe audit log + corpus
```

### Shell inside the simon container

```bash
docker compose exec simon bash         # as the non-root simon user
docker compose exec --user root simon bash
```

### Peek at state

```bash
docker compose exec simon ls /workspace/protected
docker compose exec simon cat /etc/simon/acl.json
docker compose exec simon cat /var/simon/audit.jsonl
docker compose logs simon | tail -50
```

### Regenerate the corpus (deterministic)

```bash
python3 scripts/prep_hansard.py        # writes demo/corpus/
```

### Rebuild the pitch deck

```bash
python3 scripts/build_presentation.py  # → docs/simon/simon-demo.pptx
```

### Run one Simon query as a specific demo user

```bash
# Mint an unsigned JWT for the test-only short-circuit.
mint() {
  local sub="$1"
  local H P
  H=$(python3 -c 'import base64; print(base64.urlsafe_b64encode(b"{\"alg\":\"none\",\"typ\":\"JWT\"}").rstrip(b"=").decode(),end="")')
  P=$(python3 -c "import base64,json,time; print(base64.urlsafe_b64encode(json.dumps({'sub':'$sub','email':'${sub%-*}@simon.demo','name':'${sub%-*}','iat':int(time.time()),'exp':int(time.time())+3600,'groups':[]},separators=(',',':')).encode()).rstrip(b'=').decode(),end='')")
  printf '%s.%s.' "$H" "$P"
}

set -a; source .env; set +a
docker compose exec \
  -e SIMON_OIDC_ID_TOKEN="$(mint alice-protected)" \
  -e GOOGLE_API_KEY="$GEMINI_API_KEY" \
  simon simon --provider google -m gemini-2.5-flash \
  -p 'Read one file in /workspace/protected/ and summarise it.'
```

---

## Gotchas we've already hit (don't re-hit)

- **libssl soname mismatch.** Builder (`rust:1-bullseye`) uses libssl1.1. Runtime must match (`debian:bullseye-slim`) or the binary fails at startup with `libssl.so.1.1: cannot open shared object file`. Keep both on bullseye or move both to bookworm together.
- **Host port 8080 collision.** `oblique-lawyer-app-1` already binds 8080 on this host. Simon publishes on `${SIMON_OIDC_HOST_PORT:-8765}` instead. Override with `SIMON_OIDC_HOST_PORT=...` if 8765 is also taken.
- **PID 1 = `tini sleep infinity`.** The simon container's main process is an idle sleeper. Real invocations run through `docker compose exec simon simon ...`. This is the intentional model for both interactive login and the smoke test.
- **`internal: true` silently drops `ports:`.** `simon-internal` is marked `internal: true`, so any service attached only to it cannot publish host ports. Dex must be on `simon-egress` too. Keep this in mind if you add a new service that needs a host port.
- **Dex issuer vs host DNS.** Dex's `issuer: http://dex:5556/dex` means both the container and the host browser need `dex` to be resolvable. Inside the container, Docker DNS handles it. On the host, `/etc/hosts` needs `127.0.0.1 dex`. The quick-start includes this step.
- **`--provider google` is required when using the Gemini key.** Simon defaults to `anthropic` and will fail "No API key found" on startup even with `GOOGLE_API_KEY` set if no provider is explicitly selected. The smoke test and the quick-start examples set `--provider google -m gemini-2.5-flash`.
- **`ANTHROPIC_API_KEY=""` is NOT the same as unset** from Simon's point of view — it still selects the Anthropic provider and then fails auth. The compose file passes through only the keys that are genuinely set on the host.
- **Smoke test requires a real LLM to run.** The gate only fires once the model decides to call a tool. Zero audit events on the smoke test means either the API key is wrong or the provider is unset.

---

## What's done, what's next

All 10 build tasks from the initial scope are completed. The stack runs end-to-end with a real LLM, a real Hansard corpus, and real ACL enforcement. See `docs/simon/simon-demo.pptx` for the pitch deck.

**High-value next work, roughly in priority order:**

1. **Layer-1 egress firewall** — the compose file has a placeholder `simon-egress` network but nothing is enforced. Real story needs host iptables/nftables rules or a K8s NetworkPolicy. Without this, a compromised Simon process can exfiltrate to arbitrary URLs via `web_fetch` (which is gated, but only on URL hostname). Draft rule: allow only `api.anthropic.com`, `generativelanguage.googleapis.com`, and the NES endpoint; deny all else.
2. **Seccomp profile** — deny `symlink(2)`, `link(2)`, `mount(2)`, `ptrace(2)`, `userfaultfd(2)` from inside the simon container. Closes the symlink-traversal bypass.
3. **Per-clearance bind mount** — currently the whole corpus is mounted at `/workspace` and ACL enforcement is only at L2. A per-session init container that assembles a `/workspace/` containing only the subtrees the user's clearance allows would make L1 do real work.
4. **`--gate-test` subcommand** — a Rust subcommand that takes a `ResourceRef` + `Operation` and calls `acl_gate` directly, no LLM. Makes the smoke test deterministic, no-API-key, and CI-friendly.
5. **Remove the `/etc/hosts` hack** — add a rewrite step in `simon_login.rs` that transparently swaps the IdP hostname (`dex` → `localhost`) before opening the browser, while keeping the token exchange on the internal hostname. ~20 lines.
6. **Interactive demo via Dex's native UI** — confirm the browser-login dance actually works end-to-end (the plumbing is in place, not yet walked through with a real user + real token exchange).
7. **GovAI Brokerage provider stub** — a Rust provider trait impl that points at `api.govai.gov.au` and shows the "one env var to production" story. Even a stub that documents the HTTP shape is useful for the pitch.
8. **MCP story** — currently disabled entirely. For production, either a per-MCP-tool ACL shim or a hard "MCP disabled" stance with enterprise sign-off.

Honest limits are enumerated in `docs/simon/threat-model.md`. The ones not in the "next work" list above are either out of scope (prompt-injection-induced authorised-but-undesirable actions) or require hardware-backed trust (Secret/Top Secret).

---

## Pointers

- **Positioning one-pager:** `docs/simon/pitch.md`
- **Pitch deck (14 slides):** `docs/simon/simon-demo.pptx`
- **Essential Eight Control 4 mapping:** `docs/simon/essential-eight-control-4.md`
- **OWASP LLM Top 10 coverage:** `docs/simon/owasp-llm-top-10.md`
- **Honest threat model:** `docs/simon/threat-model.md`
- **Deploy-stack README:** `deploy/README.md`
- **Corpus prep notes:** `scripts/prep_hansard_README.md`
- **Project memory (this developer's context):** `~/.claude/projects/-home-edgardo-projectsd-GovBot/memory/project_simon.md`

---

## License

GPL-3.0 — inherited from upstream `claurst`. Both the upstream project and this fork are clean-room reimplementations of Claude Code's behaviour; no proprietary Anthropic source was carried forward. See the upstream README for the `Phoenix v. IBM` / `Baker v. Selden` legal framing.

Simon-specific code (everything under `src-rust/crates/acl/`, `crates/cli/src/simon_login.rs`, `deploy/`, `scripts/`, `demo/corpus/`, `docs/simon/`) is released under the same licence.
