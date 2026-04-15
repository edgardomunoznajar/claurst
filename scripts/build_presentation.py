#!/usr/bin/env python3
"""
scripts/build_presentation.py — generate docs/simon/simon-demo.pptx.

The slide deck is the source-of-truth pitch for Simon: a Rust-based
ACL-bound CLI coding agent for Australian government agencies. Deck
targets an APS agency leader with ~15 minutes: what Simon is, why now,
what the demo proves, what the honest limits are.

Run:

    python3 scripts/build_presentation.py

Produces docs/simon/simon-demo.pptx. Idempotent — re-running overwrites.
Edit this script to change the deck; the .pptx is a build artefact.
"""

from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Inches, Pt

# ---------------------------------------------------------------------------
# Colours — a restrained palette. Charcoal on off-white with Commonwealth
# green as the single accent. Avoids the Claude-fingerprint vibes.
# ---------------------------------------------------------------------------

BG = RGBColor(0xFA, 0xFA, 0xF7)          # paper
INK = RGBColor(0x1C, 0x1C, 0x1E)         # near-black
MUTED = RGBColor(0x55, 0x55, 0x5A)       # secondary text
ACCENT = RGBColor(0x00, 0x5A, 0x43)      # deep green
ACCENT_SOFT = RGBColor(0xD4, 0xE6, 0xDF) # pale green fill
RULE = RGBColor(0xC6, 0xC6, 0xC2)        # hairlines

SLIDE_W = Inches(13.333)
SLIDE_H = Inches(7.5)

# ---------------------------------------------------------------------------
# Deck content. Edit here, not in the rendering code below.
# ---------------------------------------------------------------------------

DECK = [
    # 1 — title
    {
        "kind": "title",
        "eyebrow": "Project Simon",
        "title": "AI agents,\nunder your ACLs.",
        "subtitle": (
            "A Rust-based coding agent for Australian government agencies. "
            "Per-call ACL enforcement, tied to your SSO and enterprise "
            "search — prompt-injection-immune on the access dimension by "
            "construction."
        ),
        "footer": "docker compose up  •  github.com/edgardomunoznajar/claurst (branch: simon)",
    },
    # 2 — the moment
    {
        "kind": "section",
        "eyebrow": "The moment — April 2026",
        "title": "Agents are here. Governance isn't.",
        "bullets": [
            "Anthropic shipped Claude Mythos in April 2026. It completed a "
            "32-step autonomous network attack 3/10 times when directed.",
            "The DTA's Policy for Responsible AI in Government took effect "
            "15 December 2025. It relies on review committees, risk boards, "
            "and periodic assessments.",
            "Agents operate continuously. Governance does not.",
            "92% of organisations say agent governance is critical. Fewer "
            "than half have a formal policy. 80% report unexpected data "
            "access by agents (SailPoint / SecurityBrief AU).",
        ],
    },
    # 3 — problem
    {
        "kind": "section",
        "eyebrow": "The problem",
        "title": "An AI agent is a privileged account.",
        "bullets": [
            'ValiDATA, on Essential Eight Control 4: "An AI agent that can '
            "access systems, query databases, send emails, or modify files "
            'is, in functional terms, a privileged account."',
            "Existing CLI agents grant permissions at install time. One "
            "token, one install, ad-hoc and coarse. No per-user scoping, "
            "no per-document ACLs, no auditable denial.",
            "Prompt injection attacks don't need to defeat the agent's "
            "code — they only need to convince the LLM to ask the harness "
            "for data the user isn't cleared for.",
        ],
    },
    # 4 — thesis
    {
        "kind": "section",
        "eyebrow": "Thesis",
        "title": "Put the ACL check in code the LLM can't reach.",
        "bullets": [
            "Every read/write tool call passes through a deterministic "
            "Rust gate before any syscall. The LLM never decides whether "
            "access is allowed.",
            "Decisions are bound to an SSO-resolved principal with a PSPF "
            "clearance claim, and to a per-document ACL derived from the "
            "agency's enterprise search (NES/NQRY in the demo).",
            "Fail-closed everywhere: untagged paths default to PROTECTED, "
            "the shell defaults to Off, audit failures abort the tool call.",
            "No prompt rewording, roleplay, or agent loop can talk the "
            "check out of returning Deny.",
        ],
    },
    # 5 — architecture (diagram slide)
    {
        "kind": "architecture",
        "eyebrow": "Architecture",
        "title": "Three layers of defence.",
    },
    # 6 — layer 2 AclEnforcer
    {
        "kind": "section",
        "eyebrow": "Layer 2 — the AclEnforcer",
        "title": "Deterministic, auditable, fail-closed.",
        "bullets": [
            "Rust trait: `check(principal, resource, op) -> AclDecision`. "
            "Three backends ship: static-JSON, NES HTTP client, composite.",
            "Every decision commits to an append-only audit sink (with fsync) "
            "before the tool is allowed to act. Audit failure → tool fails.",
            "Shell mode defaults to Off. An Allowlist mode permits exact "
            "program names and denies any command containing shell "
            "metacharacters — pipes, heredocs, eval, backticks, redirection.",
            "What it does NOT claim: it cannot stop exfiltration of data "
            "the user legitimately read. That's Layer 1's job.",
        ],
    },
    # 7 — layer 1 container
    {
        "kind": "section",
        "eyebrow": "Layer 1 — container and network",
        "title": "The real trust boundary.",
        "bullets": [
            "Per-session bind mount: /workspace only contains the "
            "subtree the user's clearance allows. A misbehaving tool "
            "literally cannot see forbidden files.",
            "Egress firewall: the only path out is the LLM provider "
            "endpoint. No curl, wget, webhooks, or exfiltration channels.",
            "Seccomp denies symlink(2) and mount(2); no traversal tricks "
            "via crafted inodes.",
            "Simon binary is owned by root, read-only; the process runs "
            "as a non-root service account. Host ptrace_scope=1.",
        ],
    },
    # 8 — deploy stack
    {
        "kind": "stack",
        "eyebrow": "The demo stack",
        "title": "Single docker compose up.",
    },
    # 9 — corpus
    {
        "kind": "section",
        "eyebrow": "The corpus",
        "title": "Real Australian parliamentary text.",
        "bullets": [
            "60 speeches from Hansard, sitting day 2022-09-08. Pulled "
            "from Zenodo (Katz & Alexander 2023, DOI 10.5281/zenodo.8121950) "
            "via HTTP range requests — 400 KB out of a 329 MB archive.",
            "Each speech carries a synthetic PSPF classification in a "
            "sidecar .acl.json: Unofficial (27), Official (13), "
            "Official:Sensitive (10), Protected (10).",
            "Classification assigned by keyword heuristic, then forcibly "
            "balanced so every tier has ≥10 documents. Balancer-promoted "
            "docs are flagged for auditor honesty.",
            "The audience will recognise the speakers. That matters.",
        ],
    },
    # 10 — demo flow
    {
        "kind": "section",
        "eyebrow": "The demo flow",
        "title": "Same query. Three users. Three answers.",
        "bullets": [
            'sarah-unofficial asks: "summarise any cabinet memo in '
            '/workspace/protected". The gate denies. The LLM reports the '
            "denial with reason. Audit log records it.",
            "bill-official asks the same thing. Same outcome — his "
            "clearance is one tier too low. Different audit entry.",
            "alice-protected asks the same thing. The gate allows. The "
            "LLM reads the document and produces a summary. Audit entry "
            "recorded as Allow.",
            "docker compose exec simon tail -f /var/simon/audit.jsonl "
            "shows the decisions landing in real time.",
        ],
    },
    # 11 — coverage map
    {
        "kind": "coverage",
        "eyebrow": "Coverage map",
        "title": "What Simon buys you, framework by framework.",
    },
    # 12 — honest limits
    {
        "kind": "section",
        "eyebrow": "Honest limits",
        "title": "What Simon does NOT solve.",
        "bullets": [
            "Software-only ACL cannot be fully bypass-proof. A sufficiently "
            "motivated agent will find the narrow gaps. That's why Layer 1 "
            "exists.",
            "MCP is disabled in the demo. Every MCP tool call is an "
            "ungated external file read; production requires a "
            "per-server ACL shim.",
            "Simon does not stop prompt injection that steers the agent "
            "toward authorised-but-undesirable actions. That is a "
            "separate problem.",
            "Secret and Top Secret are not in scope. Hardware-isolated "
            "classifications need hardware-isolated harnesses.",
            "Every one of these limits is documented in docs/simon/threat-model.md.",
        ],
    },
    # 13 — production path
    {
        "kind": "section",
        "eyebrow": "From demo to production",
        "title": "One env var to the GovAI Brokerage.",
        "bullets": [
            "LLM provider: SIMON_PROVIDER=anthropic (demo) → "
            "SIMON_PROVIDER=govai pointed at api.govai.gov.au. The rest "
            "of the stack is identical.",
            "Identity: swap Dex for your agency IdP (Entra, VANguard, "
            "myGovID). Simon reads clearance from an OIDC claim.",
            "ACL source: swap nes-mock for a live NES/NQRY deployment. "
            "The wire protocol is a two-endpoint POST API — trivial to "
            "adapt to any enterprise search ACL provider.",
            "Egress firewall enforced at the host network layer by the "
            "agency's existing iptables/nftables/NetworkPolicy stack. "
            "Docker Desktop can't enforce this; production can.",
        ],
    },
    # 14 — CTA
    {
        "kind": "cta",
        "eyebrow": "What's next",
        "title": "docker compose up.",
        "bullets": [
            "The entire stack, including the Rust build, 60-document "
            "corpus, Dex, and mock NES, is one command on a laptop.",
            "scripts/smoke_test.sh asserts the gate behaviour end-to-end "
            "against the audit log — no LLM-quality assertions, just "
            "harness guarantees.",
            "Code and docs on the simon branch. Four supporting "
            "documents already shipped: Essential Eight Control 4 "
            "mapping, OWASP LLM Top 10 coverage, threat model, pitch.",
            "Next: a 30-minute walkthrough with one of your teams, and "
            "a conversation about which data sources to wire first.",
        ],
    },
]

# ---------------------------------------------------------------------------
# Rendering helpers.
# ---------------------------------------------------------------------------


def set_bg(slide, colour: RGBColor) -> None:
    bg = slide.background
    fill = bg.fill
    fill.solid()
    fill.fore_color.rgb = colour


def add_text(slide, left, top, width, height, text, *, size=18,
             bold=False, colour=INK, align=PP_ALIGN.LEFT, font="Inter"):
    tb = slide.shapes.add_textbox(left, top, width, height)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = Emu(0)
    tf.margin_right = Emu(0)
    tf.margin_top = Emu(0)
    tf.margin_bottom = Emu(0)
    lines = text.split("\n") if isinstance(text, str) else list(text)
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        run = p.add_run()
        run.text = line
        run.font.name = font
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = colour
    return tb


def add_rule(slide, left, top, width, *, colour=RULE, weight_pt=1.0):
    line = slide.shapes.add_connector(1, left, top, left + width, top)
    line.line.color.rgb = colour
    line.line.width = Pt(weight_pt)
    return line


def add_chrome(slide, slide_num: int, total: int) -> None:
    """Consistent header rule + page number for every non-title slide."""
    add_rule(slide, Inches(0.6), Inches(0.75), Inches(12.13))
    add_text(
        slide,
        Inches(11.8),
        Inches(7.05),
        Inches(1.1),
        Inches(0.3),
        f"{slide_num:02d} / {total:02d}",
        size=10,
        colour=MUTED,
        align=PP_ALIGN.RIGHT,
    )
    add_text(
        slide,
        Inches(0.6),
        Inches(7.05),
        Inches(6),
        Inches(0.3),
        "Simon  •  ACL-bound AI for Australian government",
        size=10,
        colour=MUTED,
    )


def render_title(slide, content):
    set_bg(slide, BG)
    add_text(
        slide,
        Inches(0.8),
        Inches(0.9),
        Inches(6),
        Inches(0.4),
        content["eyebrow"].upper(),
        size=13,
        colour=ACCENT,
        bold=True,
    )
    # Big title
    add_text(
        slide,
        Inches(0.8),
        Inches(1.5),
        Inches(11.5),
        Inches(3.2),
        content["title"],
        size=72,
        bold=True,
        colour=INK,
    )
    # Subtitle
    add_text(
        slide,
        Inches(0.8),
        Inches(4.9),
        Inches(11),
        Inches(1.8),
        content["subtitle"],
        size=20,
        colour=MUTED,
    )
    add_rule(slide, Inches(0.8), Inches(6.9), Inches(11.7))
    add_text(
        slide,
        Inches(0.8),
        Inches(7.05),
        Inches(11.7),
        Inches(0.3),
        content["footer"],
        size=11,
        colour=MUTED,
    )


def render_section(slide, content, slide_num, total):
    set_bg(slide, BG)
    add_chrome(slide, slide_num, total)
    add_text(
        slide,
        Inches(0.6),
        Inches(0.35),
        Inches(12),
        Inches(0.35),
        content["eyebrow"].upper(),
        size=11,
        colour=ACCENT,
        bold=True,
    )
    add_text(
        slide,
        Inches(0.6),
        Inches(1.0),
        Inches(12),
        Inches(1.3),
        content["title"],
        size=42,
        bold=True,
        colour=INK,
    )

    # Bullets with hanging indent
    top = Inches(2.6)
    for i, b in enumerate(content["bullets"]):
        # Accent square marker
        marker = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            Inches(0.65),
            top + Inches(0.18),
            Inches(0.12),
            Inches(0.12),
        )
        marker.fill.solid()
        marker.fill.fore_color.rgb = ACCENT
        marker.line.fill.background()
        add_text(
            slide,
            Inches(1.0),
            top,
            Inches(11.7),
            Inches(1.1),
            b,
            size=18,
            colour=INK,
        )
        top += Inches(1.0)


def render_architecture(slide, content, slide_num, total):
    set_bg(slide, BG)
    add_chrome(slide, slide_num, total)
    add_text(
        slide,
        Inches(0.6),
        Inches(0.35),
        Inches(12),
        Inches(0.35),
        content["eyebrow"].upper(),
        size=11,
        colour=ACCENT,
        bold=True,
    )
    add_text(
        slide,
        Inches(0.6),
        Inches(1.0),
        Inches(12),
        Inches(1.0),
        content["title"],
        size=42,
        bold=True,
        colour=INK,
    )

    # Three stacked layer boxes, L3 at top, L1 at bottom (per convention:
    # L1 is closest to hardware).
    layers = [
        (
            "Layer 3",
            "User-approval prompt (UX)",
            "Existing TUI permission dialog. Destructive ops (e.g. file_write) "
            "still prompt the operator. Can't override an L2 Deny.",
        ),
        (
            "Layer 2",
            "AclEnforcer (deterministic Rust gate)",
            "Per-call check, fail-closed, audited before the tool acts. "
            "SSO principal × PSPF-classified resource × op → Allow / Deny. "
            "Prompt-injection-immune on the access dimension.",
        ),
        (
            "Layer 1",
            "Container FS view + egress firewall + seccomp",
            "Real trust boundary. Per-clearance bind mount, deny-by-default "
            "egress, no symlink creation. Data the user can't see is NOT "
            "on the filesystem inside the container.",
        ),
    ]
    top = Inches(2.4)
    for label, heading, body in layers:
        box = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE,
            Inches(0.6),
            top,
            Inches(12.1),
            Inches(1.3),
        )
        box.fill.solid()
        box.fill.fore_color.rgb = ACCENT_SOFT
        box.line.color.rgb = ACCENT
        box.line.width = Pt(1.25)
        box.text_frame.margin_left = Inches(0.25)
        box.text_frame.margin_right = Inches(0.25)
        box.text_frame.margin_top = Inches(0.12)
        box.text_frame.word_wrap = True
        tf = box.text_frame
        p0 = tf.paragraphs[0]
        r = p0.add_run()
        r.text = f"{label}  ·  {heading}"
        r.font.name = "Inter"
        r.font.size = Pt(18)
        r.font.bold = True
        r.font.color.rgb = INK
        p1 = tf.add_paragraph()
        r2 = p1.add_run()
        r2.text = body
        r2.font.name = "Inter"
        r2.font.size = Pt(13)
        r2.font.color.rgb = MUTED
        top += Inches(1.45)


def render_stack(slide, content, slide_num, total):
    set_bg(slide, BG)
    add_chrome(slide, slide_num, total)
    add_text(
        slide,
        Inches(0.6),
        Inches(0.35),
        Inches(12),
        Inches(0.35),
        content["eyebrow"].upper(),
        size=11,
        colour=ACCENT,
        bold=True,
    )
    add_text(
        slide,
        Inches(0.6),
        Inches(1.0),
        Inches(12),
        Inches(1.0),
        content["title"],
        size=42,
        bold=True,
        colour=INK,
    )

    services = [
        ("simon", "Rust TUI agent", "Built from src-rust/, multi-stage "
         "Dockerfile, bullseye-slim runtime, non-root uid 1000, binary "
         "owned by root."),
        ("dex", "OIDC provider", "ghcr.io/dexidp/dex:v2.40.0 with three "
         "static users. Clearance encoded in the sub claim "
         "(sarah-unofficial, bill-official, alice-protected)."),
        ("nes-mock", "Mock NES ACL service", "FastAPI service that "
         "mirrors the Rust StaticJsonEnforcer. Two endpoints: "
         "POST /acl/check and POST /acl/filter."),
        ("corpus-init", "One-shot seeder", "Copies the Hansard corpus "
         "into the corpus named volume at startup, then exits."),
    ]
    top = Inches(2.35)
    for name, heading, body in services:
        pill = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE,
            Inches(0.6),
            top,
            Inches(2.6),
            Inches(0.95),
        )
        pill.fill.solid()
        pill.fill.fore_color.rgb = ACCENT
        pill.line.fill.background()
        ptf = pill.text_frame
        ptf.margin_left = Inches(0.2)
        ptf.margin_top = Inches(0.2)
        p0 = ptf.paragraphs[0]
        r0 = p0.add_run()
        r0.text = name
        r0.font.name = "JetBrains Mono"
        r0.font.size = Pt(20)
        r0.font.bold = True
        r0.font.color.rgb = BG
        p1 = ptf.add_paragraph()
        r1 = p1.add_run()
        r1.text = heading
        r1.font.name = "Inter"
        r1.font.size = Pt(11)
        r1.font.color.rgb = ACCENT_SOFT

        add_text(
            slide,
            Inches(3.4),
            top + Inches(0.1),
            Inches(9.3),
            Inches(0.9),
            body,
            size=15,
            colour=INK,
        )
        top += Inches(1.1)


def render_coverage(slide, content, slide_num, total):
    set_bg(slide, BG)
    add_chrome(slide, slide_num, total)
    add_text(
        slide,
        Inches(0.6),
        Inches(0.35),
        Inches(12),
        Inches(0.35),
        content["eyebrow"].upper(),
        size=11,
        colour=ACCENT,
        bold=True,
    )
    add_text(
        slide,
        Inches(0.6),
        Inches(1.0),
        Inches(12.1),
        Inches(1.0),
        content["title"],
        size=38,
        bold=True,
        colour=INK,
    )

    rows = [
        ("Essential Eight Control 4", "Restrict Administrative Privileges",
         "Implemented — per-session service-account principal, scoped "
         "read/write ACLs, audit register, revocable at clearance expiry."),
        ("Essential Eight Control 6", "Multi-Factor Authentication",
         "Implemented via OIDC to your IdP; destructive ops also gated "
         "by the L3 permission dialog."),
        ("OWASP LLM01", "Prompt Injection",
         "Immune-by-construction on the ACL dimension. The check runs "
         "in Rust the model cannot see or reason over."),
        ("OWASP LLM06", "Sensitive Information Disclosure",
         "Mitigated — the core value proposition. Data the user can't "
         "see is not returned, and denied attempts are audited."),
        ("OWASP LLM08", "Excessive Agency",
         "Mitigated — per-tool ACL gate, shell_mode defaults to Off, "
         "egress-locked container network."),
        ("OWASP LLM03", "Training Data Poisoning",
         "Out of scope. Mitigated upstream by the model provider."),
    ]
    top = Inches(2.25)
    for left, mid, right in rows:
        row_bg = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            Inches(0.6),
            top,
            Inches(12.1),
            Inches(0.7),
        )
        row_bg.fill.solid()
        row_bg.fill.fore_color.rgb = BG
        row_bg.line.color.rgb = RULE
        row_bg.line.width = Pt(0.5)
        add_text(
            slide,
            Inches(0.75),
            top + Inches(0.12),
            Inches(3.0),
            Inches(0.5),
            left,
            size=13,
            bold=True,
            colour=ACCENT,
        )
        add_text(
            slide,
            Inches(3.85),
            top + Inches(0.12),
            Inches(3.2),
            Inches(0.5),
            mid,
            size=13,
            bold=True,
            colour=INK,
        )
        add_text(
            slide,
            Inches(7.15),
            top + Inches(0.12),
            Inches(5.5),
            Inches(0.5),
            right,
            size=12,
            colour=MUTED,
        )
        top += Inches(0.75)


def render_cta(slide, content, slide_num, total):
    # Same as a section slide but with a big mono-font command and an
    # accent-coloured backdrop.
    set_bg(slide, BG)
    add_chrome(slide, slide_num, total)
    add_text(
        slide,
        Inches(0.6),
        Inches(0.35),
        Inches(12),
        Inches(0.35),
        content["eyebrow"].upper(),
        size=11,
        colour=ACCENT,
        bold=True,
    )
    # The command itself, rendered large in a mono font.
    add_text(
        slide,
        Inches(0.6),
        Inches(1.1),
        Inches(12),
        Inches(1.3),
        content["title"],
        size=56,
        bold=True,
        colour=ACCENT,
        font="JetBrains Mono",
    )
    top = Inches(3.0)
    for b in content["bullets"]:
        marker = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            Inches(0.65),
            top + Inches(0.18),
            Inches(0.12),
            Inches(0.12),
        )
        marker.fill.solid()
        marker.fill.fore_color.rgb = ACCENT
        marker.line.fill.background()
        add_text(
            slide,
            Inches(1.0),
            top,
            Inches(11.7),
            Inches(0.9),
            b,
            size=17,
            colour=INK,
        )
        top += Inches(0.95)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build():
    prs = Presentation()
    prs.slide_width = SLIDE_W
    prs.slide_height = SLIDE_H

    blank_layout = prs.slide_layouts[6]  # blank
    total = len(DECK)
    for i, content in enumerate(DECK, start=1):
        slide = prs.slides.add_slide(blank_layout)
        kind = content["kind"]
        if kind == "title":
            render_title(slide, content)
        elif kind == "section":
            render_section(slide, content, i, total)
        elif kind == "architecture":
            render_architecture(slide, content, i, total)
        elif kind == "stack":
            render_stack(slide, content, i, total)
        elif kind == "coverage":
            render_coverage(slide, content, i, total)
        elif kind == "cta":
            render_cta(slide, content, i, total)
        else:
            raise ValueError(f"unknown slide kind: {kind}")

    out = Path(__file__).resolve().parent.parent / "docs" / "simon" / "simon-demo.pptx"
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(out)
    size_kb = out.stat().st_size / 1024
    print(f"wrote {out} ({size_kb:.1f} KB, {total} slides)")


if __name__ == "__main__":
    build()
