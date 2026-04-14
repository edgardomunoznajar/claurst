# Simon — the ninety-second version

**The moment.** Claude Mythos shipped in April 2026. The DTA's *Policy for
the responsible use of AI in government* took effect on 15 December 2025.
92% of organisations say AI-agent governance is critical and fewer than
half have a formal policy; 80% report agents taking unintended actions,
including accessing or sharing data in ways that were not expected
([SecurityBrief AU](https://securitybrief.com.au/story/ai-agents-are-joining-the-public-service-who-s-governing-them)).
Incumbents are selling bespoke integration contracts at six-figure prices.

**The gap.** ValiDATA's mapping of the Essential Eight to AI is blunt:
"an AI agent that can access systems, query databases, send emails, or
modify files is, in functional terms, a privileged account"
([ValiDATA](https://www.validata.ai/post/ai-and-the-essential-eight-applying-australia-s-cybersecurity-framework-to-ai)).
Maturity Level 3 of Control 4 therefore demands per-agent least-privilege
with auditable denials, and the OWASP LLM Top 10 names prompt injection
(LLM01) and sensitive information disclosure (LLM06) as the concrete
attacks a privileged agent must be protected against. No existing
general-purpose CLI coding agent implements either one at the tool layer.
AI agents operate continuously; Australian agency governance cycles are
periodic by design. The gap is widening every week.

**Simon.** A Rust binary — forked from `claurst`, a clean-room Rust
reimplementation of Claude Code — that wraps a coding agent in a
per-call ACL gate tied to the agency's SSO and its enterprise-search
ACL provider (NES / NQRY). The check is deterministic Rust code that
the language model cannot reach, so Simon is prompt-injection-immune on
the ACL dimension by construction. It ships as a `docker-compose` demo
with Dex for OIDC, a FastAPI NES mock, and the Australian Hansard
1998-2022 corpus stamped with synthetic PSPF classifications. One
environment variable switches it from Anthropic direct to the GovAI
Brokerage API for a real deployment.

**Three bullets.**

- A user with Unofficial clearance asks about a cabinet memo. The
  `StaticJsonEnforcer` denies the read, the user sees a plain-English
  reason, and the append-only audit log records subject, session, and
  resource.
- A user with Protected clearance asks the same question. The gate
  Allows, the document is read, the answer is synthesised.
- Both decisions show up in the audit stream in real time, tagged with
  the session id the OIDC login minted.

**Call to action.** `docker compose up`.

**Further reading.** [essential-eight-control-4.md](essential-eight-control-4.md)
for the Control 4 mapping with code references;
[owasp-llm-top-10.md](owasp-llm-top-10.md) for the OWASP coverage table;
[threat-model.md](threat-model.md) for the honest list of known bypasses
and the defence-in-depth layout.
