# Simon and the OWASP LLM Top 10

Essential Eight Control 4 covers the privilege side of agent governance;
this document covers the AI-specific attack surface. The ten rows below
come from the [OWASP Top 10 for Large Language Model Applications](https://owasp.org/www-project-top-10-for-large-language-model-applications/)
(v1.1). Each row names Simon's coverage and points at the Rust file and
line that carries the weight.

Read alongside [essential-eight-control-4.md](essential-eight-control-4.md)
and [threat-model.md](threat-model.md).

| ID | Risk | Coverage | Mechanism | Honest note |
|---|---|---|---|---|
| **LLM01** | **Prompt Injection** — crafted inputs steer the model into unauthorised actions. | **Immune by construction** on the ACL dimension. | Every read/write/execute path calls `ToolContext::acl_gate` (`crates/tools/src/lib.rs:315`), which delegates to `AclEnforcer::check` (`crates/acl/src/lib.rs:221`). The check is deterministic Rust; the prompt cannot reach the check. `StaticJsonEnforcer::check` (`crates/acl/src/static_json.rs:256`) builds the decision from the immutable `SimonPrincipal::clearance` and a path/host lookup. | Prompt injection can still steer the agent toward *authorised-but-undesirable* actions (noisy refactors, off-task work, attempts at social engineering through its replies). That is a separate problem and Simon does not claim to solve it. |
| **LLM02** | **Insecure Output Handling** — downstream systems trust the model's output too eagerly. | **Mitigated**. | The existing `PermissionHandler` layer (`crates/tools/src/lib.rs:259` and the `check_permission` / `check_permission_with_details` helpers) prompts the user before destructive operations. It runs after an `Allow` from the ACL gate and cannot weaken it. | Output destined for other systems (shells, scripts, downstream automations) still needs the caller's own output handling. Simon cannot vouch for content downstream of `tool_result`. |
| **LLM03** | **Training Data Poisoning** — tampered training data impairs model behaviour. | **Out of scope.** | Not addressed. Simon is a harness around a hosted model; the training pipeline is the provider's concern. | Agencies reduce exposure here by choosing a provider with documented training data provenance. The Simon codebase has no lever. |
| **LLM04** | **Model Denial of Service** — resource-heavy inputs disrupt service. | **Partial.** | The `CostTracker` in `ToolContext` (`crates/tools/src/lib.rs:223`) caps per-session spend, which bounds total tokens. `NesHttpEnforcer` has a five-second timeout per ACL check (`crates/acl/src/nes_client.rs:32`) so a slow policy service cannot stall a session indefinitely. | Simon does not rate-limit model calls on its own; an aggressive user can still burn the provider quota. Pair with an API gateway. |
| **LLM05** | **Supply Chain Vulnerabilities** — compromised components undermine integrity. | **Mitigated by build posture.** | The binary is owned by root and read-only inside the container. `cargo audit` plus reproducible builds gate the ingredients. The `CompositeAllEnforcer` (`crates/acl/src/lib.rs:258`) lets a deployment layer a local static allowlist over a remote NES policy so that a compromised NES cannot unilaterally unlock data. | A compiled-in crate that talks to the network without going through the trait would bypass the gate. [threat-model.md](threat-model.md#known-bypasses) calls this out explicitly. |
| **LLM06** | **Sensitive Information Disclosure** — models leak data they should not return. | **Mitigated.** This is the core value proposition. | Data that exceeds the user's clearance never reaches the process: the container bind-mounts only the per-clearance corpus subtree, and `StaticJsonEnforcer::check` denies any path whose `required_for_file` exceeds `SimonPrincipal::clearance` (`crates/acl/src/static_json.rs:262`). Denies are logged to the audit sink (`crates/acl/src/audit.rs:16`) so an incident review can prove the refusal. | Sub-agent message-passing carries already-read content in plaintext. The content-layer defence against sub-agent leakage is the container FS view — data the sub-agent cannot see was never loaded in the first place. |
| **LLM07** | **Insecure Plugin Design** — LLM plugins process untrusted input with weak access control. | **Mitigated by default; MCP disabled in the demo.** | `ToolContext::mcp_manager` is `Option` (`crates/tools/src/lib.rs:230`). In the demo deployment it is `None`: every MCP call would otherwise be an ungated file read. Production deployments that re-enable MCP must wrap each MCP tool in a shim that routes through `ToolContext::acl_gate`. | Simon does not (yet) provide that shim. Until it does, leaving MCP off is the supported posture. |
| **LLM08** | **Excessive Agency** — unchecked autonomy causes unintended actions. | **Mitigated.** | Three layers. (a) Per-tool ACL gate as above. (b) `ShellMode::Off` as the default in `crates/acl/src/static_json.rs:74`, so bash is denied unless a policy explicitly enables an allowlist, and the `shell_allowlist_rejects_metacharacters` test at `crates/acl/src/static_json.rs:425` pins the rejection of pipes / backticks / redirection / heredocs. (c) Egress firewall at the container layer: the only outbound host is the LLM provider endpoint. | A `Deny` decision for an irreversible action is logged but not undone — a tool that happens to make a permanent external change between `Allow` and the side-effect is on its own. |
| **LLM09** | **Overreliance** — users trust outputs too much. | **Out of scope.** | A training and UX concern. Simon displays the clearance label and denial reasons verbatim in `acl_gate` (`crates/tools/src/lib.rs:320`) so the user sees why an action was refused, which supports calibrated trust, but it does not police overreliance on allowed outputs. | Agencies handle this through user training, not harness code. |
| **LLM10** | **Model Theft** — proprietary weights are exfiltrated. | **Out of scope.** | Simon runs against a hosted provider; there are no weights on the host to steal. | Agencies hosting their own weights need a separate control. |

## Summary of coverage claim

- **LLM01**, **LLM06**, **LLM08** — the three risks that map most tightly
  to per-agent least-privilege — are exactly the rows where Simon carries
  weight through the Rust ACL gate.
- **LLM02**, **LLM04**, **LLM05**, **LLM07** are mitigated at the harness
  or container layer but depend on the deployment being configured the way
  the [threat model](threat-model.md#recommended-deployment-posture)
  describes.
- **LLM03**, **LLM09**, **LLM10** are honestly out of scope; listing them
  as "covered" would be dishonest and we do not.
