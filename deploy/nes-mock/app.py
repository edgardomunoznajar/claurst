"""Mock NES ACL service for the Simon demo.

Mirrors ``simon_acl::static_json::StaticJsonEnforcer`` from the Rust
workspace, because the demo uses the same policy file format on both
sides (``/etc/nes/policy.json`` here, ``/etc/simon/acl.json`` over in
the Simon container). Keep the two parsers in lockstep — if you add a
field on the Rust side, mirror it here or fail-closed.

Clearance lattice (matches ``src-rust/crates/acl/src/lib.rs``):

    unofficial          = 0
    official            = 1
    official-sensitive  = 2
    protected           = 3

The service is fail-closed: anything it cannot parse or classify is
treated as ``protected``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from fastapi import FastAPI
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nes-mock")

POLICY_PATH = os.environ.get("NES_POLICY_PATH", "/etc/nes/policy.json")

# ---------- Clearance lattice ----------

CLEARANCE_ORDER: Dict[str, int] = {
    "unofficial": 0,
    "official": 1,
    "official-sensitive": 2,
    "protected": 3,
}


def clearance_rank(label: str) -> int:
    """Return the numeric rank of a clearance label, fail-closed on unknown."""
    return CLEARANCE_ORDER.get(label, CLEARANCE_ORDER["protected"])


def dominates(principal: str, required: str) -> bool:
    return clearance_rank(principal) >= clearance_rank(required)


# ---------- Policy loading ----------

_SHELL_METACHARS = set("|&;<>`$(){}\\\"'\n\r")


class Policy:
    """In-memory representation of /etc/nes/policy.json.

    The JSON shape mirrors simon_acl::static_json::StaticPolicy exactly,
    including the shell_mode discriminator ``{"mode": "allowlist", ...}``.
    """

    def __init__(self, raw: Dict[str, Any]) -> None:
        self.file_rules: List[Tuple[str, str]] = [
            (r["prefix"], r["min_clearance"])
            for r in raw.get("file_rules", [])
        ]
        self.url_rules: List[Tuple[str, str]] = [
            (r["host"], r["min_clearance"]) for r in raw.get("url_rules", [])
        ]
        shell_mode = raw.get("shell_mode") or {"mode": "off"}
        self.shell_mode_kind: str = shell_mode.get("mode", "off")
        self.shell_programs: List[str] = list(shell_mode.get("programs", []))
        self.shell_deny_prefixes: List[str] = list(
            shell_mode.get("deny_prefixes", [])
        )
        self.default_file_min_clearance: str = raw.get(
            "default_file_min_clearance", "protected"
        )
        self.default_url_min_clearance: str = raw.get(
            "default_url_min_clearance", "protected"
        )

    def required_for_file(self, uri: str) -> str:
        best: Optional[str] = None
        for prefix, min_clearance in self.file_rules:
            if uri.startswith(prefix):
                if best is None or clearance_rank(min_clearance) > clearance_rank(best):
                    best = min_clearance
        return best if best is not None else self.default_file_min_clearance

    def required_for_url(self, uri: str) -> str:
        try:
            parsed = urlparse(uri)
        except Exception:
            return self.default_url_min_clearance
        host = parsed.hostname
        if not host:
            return self.default_url_min_clearance
        matches = [
            mc for h, mc in self.url_rules if h == host
        ]
        if not matches:
            return self.default_url_min_clearance
        return max(matches, key=clearance_rank)

    def check_shell(self, command: str) -> Tuple[bool, str]:
        """Return (allow, reason)."""
        if self.shell_mode_kind == "off":
            return False, "shell execution is disabled in this deployment"
        if self.shell_mode_kind == "allowlist":
            if any(ch in _SHELL_METACHARS for ch in command):
                return (
                    False,
                    f"shell metacharacters not permitted in allowlist mode: {command[:80]}",
                )
            parts = command.split()
            if not parts:
                return False, "empty shell command"
            argv0 = parts[0]
            program = argv0.rsplit("/", 1)[-1]
            if program in self.shell_programs:
                return True, ""
            return False, f"program `{program}` not in shell allowlist"
        if self.shell_mode_kind == "dangerous_open":
            trimmed = command.lstrip()
            for prefix in self.shell_deny_prefixes:
                if trimmed.startswith(prefix):
                    return (
                        False,
                        f"shell command starts with forbidden prefix `{prefix.rstrip()}`",
                    )
            return True, ""
        # Unknown mode — fail closed.
        return False, f"unknown shell_mode.mode: {self.shell_mode_kind}"


def load_policy(path: str) -> Policy:
    with open(path, "rb") as f:
        raw = json.load(f)
    log.info("loaded NES policy from %s", path)
    return Policy(raw)


# ---------- FastAPI surface ----------

app = FastAPI(title="Simon demo mock NES", version="0.1.0")
POLICY: Policy = load_policy(POLICY_PATH)


class Principal(BaseModel):
    subject: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    clearance: str
    groups: List[str] = Field(default_factory=list)
    session_id: Optional[str] = None


class ResourceRef(BaseModel):
    kind: str  # "file" | "directory" | "url" | "shell_command"
    uri: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class CheckRequest(BaseModel):
    principal: Principal
    resource: ResourceRef
    operation: str  # read | write | list | execute


class FilterRequest(BaseModel):
    principal: Principal
    candidates: List[ResourceRef]


def _decide(principal: Principal, resource: ResourceRef) -> Dict[str, Any]:
    kind = resource.kind
    if kind in ("file", "directory"):
        required = POLICY.required_for_file(resource.uri)
        if dominates(principal.clearance, required):
            return {"decision": "allow"}
        return {
            "decision": "deny",
            "reason": (
                f"{resource.uri} requires {required} clearance; "
                f"you have {principal.clearance}"
            ),
            "required_clearance": required,
        }
    if kind == "url":
        required = POLICY.required_for_url(resource.uri)
        if dominates(principal.clearance, required):
            return {"decision": "allow"}
        return {
            "decision": "deny",
            "reason": f"{resource.uri} requires {required} clearance",
            "required_clearance": required,
        }
    if kind == "shell_command":
        allow, reason = POLICY.check_shell(resource.uri)
        if allow:
            return {"decision": "allow"}
        return {"decision": "deny", "reason": reason}
    return {"decision": "deny", "reason": f"unknown resource kind: {kind}"}


@app.post("/acl/check")
def acl_check(req: CheckRequest) -> Dict[str, Any]:
    return _decide(req.principal, req.resource)


@app.post("/acl/filter")
def acl_filter(req: FilterRequest) -> Dict[str, Any]:
    visible: List[Dict[str, Any]] = []
    for candidate in req.candidates:
        decision = _decide(req.principal, candidate)
        if decision.get("decision") == "allow":
            visible.append(candidate.model_dump())
    return {"visible": visible}


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}
