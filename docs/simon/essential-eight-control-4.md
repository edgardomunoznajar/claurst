# Simon and Essential Eight Control 4

> "An AI agent that can access systems, query databases, send emails, or
> modify files is, in functional terms, a privileged account."
> — ValiDATA, [*AI and the Essential Eight: applying Australia's cybersecurity framework to AI*](https://www.validata.ai/post/ai-and-the-essential-eight-applying-australia-s-cybersecurity-framework-to-ai)

Simon is the Essential Eight Control 4 (Restrict Administrative Privileges)
implementation for AI coding agents deployed inside Australian government
agencies. Control 4 at Maturity Level 3 requires that every privileged
identity be dedicated, scoped, registered, revocable, credentialled without
shared secrets, and rotated through a secrets manager. Simon treats an LLM
agent session as exactly that kind of identity and enforces each of those
requirements in deterministic Rust code that the language model cannot reach.

This document is the flagship mapping. See also
[owasp-llm-top-10.md](owasp-llm-top-10.md) for the AI-specific attack
surface, [threat-model.md](threat-model.md) for the defence-in-depth layout,
and [pitch.md](pitch.md) for the ninety-second version.

## Control 4 ML3 requirements

Each row names one ML3 requirement listed by ValiDATA, the Simon mechanism
that addresses it, and the current status in the `simon` branch.

| Requirement | Simon mechanism | Status | Notes |
|---|---|---|---|
| Dedicated service account per agent | OIDC authorisation-code flow at session start resolves to a `SimonPrincipal` with an immutable `subject` claim. The `ToolContext` carries it in every tool call. See `crates/acl/src/lib.rs:74` (`SimonPrincipal`) and `crates/cli/src/main.rs:638` (placeholder wiring). | Pending | Today the CLI installs `SimonPrincipal::anonymous()` at `crates/acl/src/lib.rs:95`, which is fail-closed at `Clearance::Unofficial`. Real OIDC wiring to Dex is the next login milestone. |
| Access scoped strictly to what the agent needs | `AclEnforcer::check` runs before every read/write/execute syscall. The `ToolContext::acl_gate` helper at `crates/tools/src/lib.rs:315` is the single choke-point; each tool calls it with a `ResourceRef` built from its structured input, never from raw LLM text. Live call sites: `file_read.rs:71`, `file_edit.rs:82`, `file_write.rs:63`, `batch_edit.rs:97`, `apply_patch.rs:309`, `glob_tool.rs:70`, `grep_tool.rs:162`, `notebook_edit.rs:104`, `web_fetch.rs:309`, `bash.rs:370`, `pty_bash.rs:534`. | Implemented | A `Deny` returned from the trait is final. It cannot be overridden by the existing user-approval prompt layer. |
| Documented in the privileged account register | Append-only `AuditEvent` stream at `crates/acl/src/audit.rs:16`. Every ACL decision (Allow or Deny) carries subject, session id, email, clearance, operation, resource, decision, and enforcer backend. `TracingSink` writes JSONL to the process log; a `NullSink` exists for tests. | Partial | A synchronous, fail-closed sink that refuses to serve a tool call if the audit write fails is the next pending upgrade. Today the sink writes best-effort to `tracing`. |
| Access revocable on short notice | `SimonPrincipal::expires_at` is checked by `is_expired` at `crates/acl/src/lib.rs:88`. The session id travels with every audit event, so a revocation list can be keyed on it. | Implemented (mechanism), Pending (revocation API) | The lookup against a revocation list (either a local bitmap or an OIDC introspection call) is not yet wired. |
| No shared credentials between AI systems and human users | Every session receives its own `SimonPrincipal` (`session_id`, `subject`, `issued_at`, `expires_at`). Nothing in the process pool is shared across sessions; the principal is an `Arc` inside `ToolContext` and is constructed once per CLI invocation at `crates/cli/src/main.rs:638`. | Implemented | Service-to-service credentials (NES, LLM provider) are still container-environment variables and move to a secrets manager in the deployment posture described in [threat-model.md](threat-model.md#recommended-deployment-posture). |
| Credentials rotated through a secrets management system | Out of scope for the binary; handled by the container runtime (Docker secrets in the demo; a real KMS/HSM in production). | Pending | The demo `docker-compose` mounts a dev JWKS; a production deployment would point `SIMON_ACL_POLICY` or `SIMON_NES_URL` at paths backed by a secrets manager. |
| OAuth 2.0 client credentials / certificates / API keys, not username+password | OIDC authorisation-code flow against Dex for the interactive user session; OAuth 2.0 client-credentials for Simon-to-NES. `NesHttpEnforcer` at `crates/acl/src/nes_client.rs:24` is the HTTP client that will carry the bearer token. | Pending | Bearer-token propagation lands with the OIDC milestone. The NES client already fails closed on any non-success status at `crates/acl/src/nes_client.rs:82`, which is the required ML3 behaviour for a broken policy service. |

### Fail-closed defaults (the unglamorous half of Control 4)

Control 4 is only meaningful if the default answer is "no". Simon's defaults:

- **Untagged file → deny.** `StaticPolicy::default_file_min_clearance` is
  `Clearance::Protected` at `crates/acl/src/static_json.rs:99`. A file that
  matches no policy prefix is treated as Protected, not Unofficial. The
  accompanying test `untagged_file_denies_by_default` at
  `crates/acl/src/static_json.rs:355` pins this behaviour.
- **Untagged URL → deny.** Same default, same reason, enforced at
  `required_for_url` in `crates/acl/src/static_json.rs:160`.
- **Shell → off.** `ShellMode::default()` returns `Off` at
  `crates/acl/src/static_json.rs:74`. Every `BashTool` / `PtyBashTool` call
  on a default policy is denied. Test:
  `shell_off_by_default` at `crates/acl/src/static_json.rs:369`.
- **NES service error → deny.** `NesHttpEnforcer::check` returns
  `AclDecision::deny` on any non-success HTTP status
  (`crates/acl/src/nes_client.rs:82`). A prompt-injected attacker who
  denial-of-services NES therefore loses access rather than gains it.
- **No policy configured → deny.** When neither `SIMON_ACL_POLICY` nor
  `SIMON_NES_URL` is set, `crates/cli/src/main.rs:629` installs a
  `StaticJsonEnforcer::new(StaticPolicy::default())`, which inherits the
  Protected-minimum default above.

## Beyond Control 4

The other Essential Eight controls Simon touches:

- **Control 6, Multi-factor authentication.** Because Simon's principal is
  minted by an OIDC identity provider, MFA is whatever the IdP enforces.
  Dex in the demo wraps a simple password check; a production deployment
  points at the agency SSO and inherits its MFA policy. The Simon binary
  never sees a password.
- **Control 7, Multi-factor authentication for privileged actions.** Simon
  layers the existing `PermissionHandler` (`crates/tools/src/lib.rs:259`) on
  top of the ACL gate. Destructive operations still prompt the user for
  explicit approval even after the ACL says Allow. The prompt layer is UX,
  not security — a Deny is still final.
- **Control 2, Patching applications.** Simon ships as an immutable OCI
  image. Upgrades are image rotations; the binary is owned by root and the
  running container user has no write access to it. See the deployment
  posture section of [threat-model.md](threat-model.md#recommended-deployment-posture).
- **Control 8, Restrict Microsoft Office macros.** Not applicable to Simon;
  listed for completeness.

## What Essential Eight does not cover

ValiDATA is explicit:

> "It does not address prompt injection or other AI-specific attack vectors."

Essential Eight is a perimeter and privilege framework; prompt injection is
an in-band, inside-the-trust-boundary attack that the framework was never
designed to reason about. Simon's answer to the AI-specific layer is the
OWASP LLM Top 10 mapping in [owasp-llm-top-10.md](owasp-llm-top-10.md) and
the honest threat model in [threat-model.md](threat-model.md).
