# Simon demo stack

A single `docker compose up --build` from the repo root runs the whole
demo: Simon (Rust TUI agent), Dex (OIDC provider), a mock NES service,
and a corpus volume seeded from `demo/corpus/`.

## Prerequisites

- Docker Desktop (Linux containers mode)
- An `ANTHROPIC_API_KEY` exported in your shell. The stack will boot
  without one, but Simon will refuse to make LLM calls.
- `demo/corpus/` populated by `scripts/prep_hansard.py`. This is handled
  by a parallel branch; if the directory is empty the `corpus-init`
  container will exit with an error.

## First run

```bash
export ANTHROPIC_API_KEY=sk-ant-...
docker compose up --build
```

Expected startup order:

1. `nes-mock` and `dex` build and start.
2. `corpus-init` copies `demo/corpus/*` into the `corpus` named volume
   and exits 0. Look for `corpus-init: done. Document count by tier:`.
3. `simon` waits for both dex and nes-mock to be healthy and for
   corpus-init to exit cleanly, then attaches to your terminal (it
   owns stdin/stdout via `tty: true`, `stdin_open: true`).

## Ports exposed on the host

| Port           | Service | Purpose                                       |
|----------------|---------|-----------------------------------------------|
| `:8080`        | simon   | OIDC loopback redirect target                 |
| `127.0.0.1:5556` | dex   | Dex login UI (for the browser during OAuth)  |

`nes-mock` and `corpus-init` have no host ports.

## Logging in as each demo user

When Simon starts its OIDC flow it opens the Dex login page in your
browser (or prints a URL you can paste). Use one of:

| Username | Email              | Password    | Clearance   |
|----------|--------------------|-------------|-------------|
| sarah    | sarah@simon.demo   | `Password!1`| unofficial  |
| bill     | bill@simon.demo    | `Password!1`| official    |
| alice    | alice@simon.demo   | `Password!1`| protected   |

See `deploy/dex/README.md` for the ugly-but-functional scheme that
transmits clearance through the OIDC `sub` claim.

## The three-user, three-view demo

With the stack running:

1. Start Simon, authenticate as `sarah@simon.demo`. Ask it `list files
   under /workspace`. You should see only `unofficial/`.
2. Exit Simon, restart the stack (or clear session state), authenticate
   as `bill@simon.demo`. You should now see `unofficial/` and
   `official/`.
3. Repeat as `alice@simon.demo` — she sees all four tiers, including
   `protected/`.

Each transition demonstrates that filtering happens inside the
enforcer, not at the filesystem layer (Simon's container mounts the
whole corpus read-only and relies on the ACL gate to refuse).

## Tailing the audit log

```bash
docker compose exec simon tail -f /var/simon/audit.jsonl
```

Every tool call goes through `simon_acl::audit::FileJsonlSink` and
lands here as one JSON record per line. Preserve it between runs via
the `simon-state` named volume (declared in `docker-compose.yml`).

## Resetting state

```bash
docker compose down -v
```

Removes all three named volumes (`corpus`, `simon-state`, `dex-state`).
Next `docker compose up --build` will re-seed the corpus and Dex will
rebuild its sqlite DB from scratch.

## Networking model

Two networks:

- `simon-internal` (`internal: true`) — all four services. No path to
  the host's default bridge. Use this for all service-to-service
  traffic (Simon -> NES, Simon -> Dex).
- `simon-egress` — Simon only. Placeholder attachment point for
  host-level iptables rules that would restrict Simon's egress to the
  LLM provider's hostname.

> WARNING: `docker-compose.yml` does NOT enforce egress filtering.
> For production, pin Simon's outbound traffic at the host network
> layer (iptables, nftables, a managed firewall, or a per-pod egress
> policy in Kubernetes). Docker Desktop cannot do this for you.

## Environment variables Simon reads at startup

From `src-rust/crates/cli/src/main.rs`:

| Variable                    | Meaning                                                 |
|-----------------------------|---------------------------------------------------------|
| `SIMON_ACL_POLICY`          | Path to static ACL policy JSON. Preferred over NES.     |
| `SIMON_NES_URL`             | Remote NES ACL enforcer endpoint. Used if no local policy file exists. |
| `SIMON_AUDIT_FILE`          | Append-only JSONL audit log path. Falls back to tracing.|
| `ANTHROPIC_API_KEY`         | LLM provider credential. Passed through from the host.  |
| `SIMON_OIDC_ISSUER`         | Dex issuer URL (consumed by the parallel OIDC client).  |
| `SIMON_OIDC_CLIENT_ID`      | Static client ID declared in `deploy/dex/config.yaml`.  |
| `SIMON_OIDC_CLIENT_SECRET`  | Matching static client secret.                          |

The three `SIMON_OIDC_*` vars are set here in anticipation of the
OIDC login flow being wired in. `main.rs` does not read them yet;
that's tracked in a parallel branch.

## Rebuilding after Rust changes

```bash
docker compose build simon
docker compose up -d simon
```

The multi-stage Dockerfile does a full `cargo build --release
--package simon --no-default-features`. First build is slow (~5-10 min
cold). Subsequent builds hit Docker's layer cache if only `src-rust/`
changes.
