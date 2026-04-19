# SIM-003 — ACL coherence test matrix across all tool surfaces

**Severity:** Medium (P1) — blocks closure of SIM-001 and prevents recurrence of this class of bug.
**Reporter:** edgardo
**Component:** `crates/tools/tests/`, `crates/acl/tests/`

## Motivation

SIM-001 showed that Simon's ACL is only as strong as the tool with the weakest path check. The Read tool gated correctly; the shell tool didn't; result: full bypass. The underlying issue is architectural — every tool that resolves a path independently is a candidate for the same class of bug.

This ticket creates a single test matrix that enumerates every tool × every ACL-relevant operation × every clearance tier, and asserts coherence: for any given `(subject, resource)` pair, every tool that can reach that resource must return the same decision.

## Matrix axes

**Tools** (one row per path-consuming tool):
- `Read`
- `Glob`
- `Grep`
- `LS`
- `Bash` (per-program: `cat`, `head`, `tail`, `grep`, `ls`, `wc`, and every future addition)
- `Write` / `Edit` (when applicable)
- MCP tools that claim a `readonly` hint — verify they're actually gated

**Clearances** (one column per tier):
- `unofficial`, `official`, `official-sensitive`, `protected`
- Plus `anonymous` (fallback) and `missing` (config absent)

**Paths** (one cell per category):
- Direct prefix match
- Symlink into denied zone
- `..` traversal from allowed zone
- Absolute vs canonicalised divergence
- Unicode-normalised vs raw
- Glob expansion crossing tiers (`grep -r . /workspace/*`)

## Expected behaviour for every cell

If `file_rules` would deny the path for the subject via the Read tool, then *every* row must also deny. Every allow must also be uniform.

## Deliverable

`crates/tools/tests/acl_coherence.rs` — parameterised table test. Proptest-style for path mutation axes (symlink, traversal, glob, unicode). Fixture policy shared across the test suite.

Helper: a `TestHarness` that spins up an ephemeral Simon agent with a given policy and subject, invokes a tool, and returns `(decision, audit_entries)`. All assertions are over that pair.

## CI gate

- Test must run on every PR touching `crates/tools/`, `crates/acl/`, or `crates/api/`.
- Failure blocks merge.
- Expected runtime <10s (no network, no real LLM).

## Acceptance criteria

- Test matrix enumerates every tool currently in `crates/tools/` and covers all five clearance tiers.
- SIM-001 is reproducible as a failing test before the fix lands, and passing after.
- Adding a new tool requires adding its row to the matrix (enforced by a `#[cfg(test)]` registry of path-consuming tools, similar to how simon-core tracks tool definitions).
- Adding a new program to the shell allowlist adds a row automatically (programs iterated from policy).

## Out of scope

- Network URL rules (separate ticket if audit reveals analogous bypass in `fetch` / MCP HTTP tools).
- Write-path coherence (needed but scoped separately).

## Related

- SIM-001 (blocker — this ticket exists to verify the fix).
- SIM-002 (independent).
