# SIM-001 — Shell tool ACL bypass (confused deputy)

**Severity:** Critical (P0) — security property failure. The ACL gate is Simon's headline differentiator for gov-agency use; this bypass makes `file_rules` advisory only for any shell-allowlisted program that takes a path.
**Discovered:** 2026-04-19 via red-team prompt through the Aider → OpenAI-shim → Simon chain.
**Reporter:** edgardo
**Component:** `crates/tools/src/bash.rs`, `crates/acl/`

## Summary

The shell tool gates on command name only. `cat`, `head`, `tail`, `grep`, `ls`, `wc` (the default allowlist) all accept path arguments that are never cross-checked against `file_rules`. Result: any user can read any file on the workspace via shell, regardless of clearance, even though the Read tool correctly denies.

## Reproduction

Policy (`/etc/simon/acl.json`):
```json
{
  "file_rules": [
    {"prefix": "/workspace/unofficial/", "min_clearance": "unofficial"},
    {"prefix": "/workspace/protected/",  "min_clearance": "protected"}
  ],
  "shell_mode": {"mode": "allowlist", "programs": ["ls","head","tail","wc","cat","grep"]},
  "default_file_min_clearance": "protected"
}
```

As anonymous / UNOFFICIAL:

| Call | Expected | Actual |
|------|----------|--------|
| `Read("/workspace/protected/x.md")` | deny | deny ✅ |
| `Bash("cat /workspace/protected/x.md")` | deny | **allow** ❌ |
| `Bash("head -1 /workspace/protected/x.md")` | deny | allow ❌ |
| `Bash("grep -r . /workspace/protected/")` | deny | allow ❌ |
| `Bash("ls /workspace/protected/")` | deny | allow ❌ |

Audit log (`/var/simon/audit.jsonl`) captures the allow decision cleanly:
```json
{"subject":"anonymous","clearance":"UNOFFICIAL","operation":"execute",
 "resource":{"kind":"shell_command",
             "uri":"cat /workspace/protected/2022-09-08-anthony-albanese-...md"},
 "decision":{"decision":"allow"},"enforcer":"static-json"}
```

## Root cause

`crates/tools/src/bash.rs:370` builds `ResourceRef::shell(params.command)` with `Operation::Execute`. The static-json enforcer's only check for a `shell_command` resource is "is the leading program in the allowlist?" It never parses argv, never resolves paths, never re-evaluates `file_rules`.

## Fix

Before `acl_gate(Execute, ...)` in `BashTool::run`, add a *secondary* check:

1. Tokenise the command (shell-quoting-aware — use the `shell-words` crate).
2. Extract every argument that looks like a path:
   - Starts with `/`, `.`, `~`, or contains `/`.
   - Not a known flag (starts with `-` and not `--`).
   - Expand globs (`glob` crate) — each expansion is an independent check.
3. For each path:
   - Canonicalise relative to the tool's working dir.
   - Resolve `..` and symlinks to the absolute path.
   - Reject if canonicalisation escapes the workspace root.
4. Call `acl.check_file(canonical, Operation::Read, subject)`; any `deny` short-circuits the whole shell call with a unified deny.
5. Log each inner check as its own audit record (even when the outer `execute` is denied) so forensics can see *why* it failed.

Flags that take paths should be enumerated per-program (`grep -f`, `head --file`, `tail --follow=/path`) — otherwise attackers can hide paths behind options.

## Tests to add (blocks SIM-003)

- Unit: for each allowlisted program, path through every known flag form, each produces the expected `check_file` call with the canonical path.
- Integration: full policy fixture with clearance tiers, tries every program × every protected path via shell, asserts deny + audit entry.
- Fuzz: randomised quoting, spaces, `$(...)`, backticks, `&&` (already forbidden by parser, confirm), UTF-8 homoglyphs in paths (U+2215 etc.) — all must deny or canonicalise deterministically.

## Acceptance criteria

- All matrix cells in the reproduction table return `deny`.
- Audit entries exist for both the outer `execute` refusal *and* the inner `read` denial that drove it.
- Test harness lives at `crates/tools/tests/shell_acl_coherence.rs` and covers every program in the default allowlist.
- Policy doc updated to spell out the new semantics ("shell path arguments are gated by file_rules at read level").

## Workaround until fixed

Narrow the shell allowlist to commands that do not take file paths. Default policy should set `shell_mode.mode = "deny"` until this lands. Update the `/etc/simon/acl.json` demo fixture to reflect.

## Related

- SIM-002 (`registry.rs:144` api_key drop) — different subsystem, independent fix.
- SIM-003 (regression test matrix) — required before closing this ticket.
