//! Simon ACL enforcement.
//!
//! Every read/write tool in Simon must call [`AclEnforcer::check`] before any
//! syscall that touches data the user could be denied. The enforcer is
//! deterministic code — never LLM-mediated — and its decision is final: a
//! `Deny` cannot be overridden by a user-approval prompt. The approval prompt
//! layer exists on top for destructive-op UX, but it only runs after an
//! `Allow`.
//!
//! ## Layering
//!
//! Simon uses defense-in-depth:
//!   1. Container FS view: the running container only mounts files the user's
//!      clearance allows into `/workspace` at session start.
//!   2. `AclEnforcer`: per-tool gate that rejects before the syscall.
//!
//! This crate is layer 2.

use std::collections::HashMap;
use std::fmt;
use std::path::Path;
use std::sync::Arc;

use async_trait::async_trait;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

pub mod audit;
pub mod nes_client;
pub mod static_json;

pub use audit::{AuditEvent, AuditSink};
pub use nes_client::NesHttpEnforcer;
pub use static_json::StaticJsonEnforcer;

/// Protective Security Policy Framework classification levels that Simon
/// recognises. Secret / Top Secret are intentionally excluded — those require
/// hardware-isolated handling that no software-only demo can honestly claim.
#[derive(
    Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize,
)]
#[serde(rename_all = "kebab-case")]
pub enum Clearance {
    Unofficial = 0,
    Official = 1,
    OfficialSensitive = 2,
    Protected = 3,
}

impl Clearance {
    /// True if `self` is cleared to see data classified at `other`.
    pub fn dominates(self, other: Clearance) -> bool {
        (self as u8) >= (other as u8)
    }

    pub fn label(self) -> &'static str {
        match self {
            Clearance::Unofficial => "UNOFFICIAL",
            Clearance::Official => "OFFICIAL",
            Clearance::OfficialSensitive => "OFFICIAL:Sensitive",
            Clearance::Protected => "PROTECTED",
        }
    }
}

impl fmt::Display for Clearance {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.label())
    }
}

/// The authenticated subject for a Simon session. Constructed once, at login,
/// from the OIDC ID token. Immutable for the rest of the process.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SimonPrincipal {
    pub subject: String,
    pub display_name: String,
    pub email: String,
    pub clearance: Clearance,
    #[serde(default)]
    pub groups: Vec<String>,
    pub session_id: String,
    pub issued_at: DateTime<Utc>,
    pub expires_at: DateTime<Utc>,
}

impl SimonPrincipal {
    pub fn is_expired(&self, now: DateTime<Utc>) -> bool {
        now >= self.expires_at
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ResourceKind {
    File,
    Directory,
    Url,
    ShellCommand,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Operation {
    Read,
    Write,
    List,
    Execute,
}

/// A concrete reference to the thing the tool is about to touch. Built by the
/// tool from its structured input (never from raw LLM text).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResourceRef {
    pub kind: ResourceKind,
    /// Absolute, canonicalised path or URL. For shell commands, the command
    /// string after argv split.
    pub uri: String,
    /// Optional sidecar metadata — e.g. the `.acl.json` loaded beside a file.
    #[serde(default)]
    pub metadata: HashMap<String, serde_json::Value>,
}

impl ResourceRef {
    pub fn file(path: impl AsRef<Path>) -> Self {
        Self {
            kind: ResourceKind::File,
            uri: path.as_ref().to_string_lossy().into_owned(),
            metadata: HashMap::new(),
        }
    }

    pub fn directory(path: impl AsRef<Path>) -> Self {
        Self {
            kind: ResourceKind::Directory,
            uri: path.as_ref().to_string_lossy().into_owned(),
            metadata: HashMap::new(),
        }
    }

    pub fn url(uri: impl Into<String>) -> Self {
        Self {
            kind: ResourceKind::Url,
            uri: uri.into(),
            metadata: HashMap::new(),
        }
    }

    pub fn shell(command: impl Into<String>) -> Self {
        Self {
            kind: ResourceKind::ShellCommand,
            uri: command.into(),
            metadata: HashMap::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "decision", rename_all = "snake_case")]
pub enum AclDecision {
    Allow,
    Deny {
        reason: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        required_clearance: Option<Clearance>,
    },
}

impl AclDecision {
    pub fn is_allow(&self) -> bool {
        matches!(self, AclDecision::Allow)
    }

    pub fn deny(reason: impl Into<String>) -> Self {
        AclDecision::Deny {
            reason: reason.into(),
            required_clearance: None,
        }
    }

    pub fn deny_required(reason: impl Into<String>, required: Clearance) -> Self {
        AclDecision::Deny {
            reason: reason.into(),
            required_clearance: Some(required),
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum AclError {
    #[error("enforcer backend error: {0}")]
    Backend(String),
    #[error("resource reference is invalid: {0}")]
    InvalidResource(String),
}

/// The core trait every Simon tool depends on via DI. Implementations MUST be
/// deterministic with respect to their inputs — no LLM calls, no nondeterminism
/// that could be steered by a prompt-injected message.
#[async_trait]
pub trait AclEnforcer: Send + Sync + 'static {
    /// Decide whether `principal` may perform `op` on `resource`. This is the
    /// hard gate — it runs before any syscall.
    async fn check(
        &self,
        principal: &SimonPrincipal,
        resource: &ResourceRef,
        op: Operation,
    ) -> Result<AclDecision, AclError>;

    /// Reduce a candidate set to only resources the principal can see. Used by
    /// list/glob/grep so results are silently filtered rather than noisily
    /// denied per-hit.
    async fn filter_visible(
        &self,
        principal: &SimonPrincipal,
        candidates: Vec<ResourceRef>,
    ) -> Result<Vec<ResourceRef>, AclError> {
        let mut out = Vec::with_capacity(candidates.len());
        for c in candidates {
            if let Ok(AclDecision::Allow) =
                self.check(principal, &c, Operation::Read).await
            {
                out.push(c);
            }
        }
        Ok(out)
    }

    /// Human-readable identifier for the backend, for logs.
    fn backend_name(&self) -> &'static str;
}

/// A shared reference counted enforcer — the flavour ToolContext actually
/// carries around.
pub type SharedEnforcer = Arc<dyn AclEnforcer>;

/// Conjunction combinator: every enforcer must Allow or the decision is Deny.
/// Use when you want to layer a local static allowlist on top of a remote
/// NES policy.
pub struct CompositeAllEnforcer {
    pub backends: Vec<SharedEnforcer>,
}

impl CompositeAllEnforcer {
    pub fn new(backends: Vec<SharedEnforcer>) -> Self {
        Self { backends }
    }
}

#[async_trait]
impl AclEnforcer for CompositeAllEnforcer {
    async fn check(
        &self,
        principal: &SimonPrincipal,
        resource: &ResourceRef,
        op: Operation,
    ) -> Result<AclDecision, AclError> {
        for backend in &self.backends {
            match backend.check(principal, resource, op).await? {
                AclDecision::Allow => continue,
                deny @ AclDecision::Deny { .. } => return Ok(deny),
            }
        }
        Ok(AclDecision::Allow)
    }

    fn backend_name(&self) -> &'static str {
        "composite-all"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clearance_lattice() {
        assert!(Clearance::Protected.dominates(Clearance::Official));
        assert!(Clearance::Protected.dominates(Clearance::Protected));
        assert!(!Clearance::Official.dominates(Clearance::Protected));
        assert!(!Clearance::Unofficial.dominates(Clearance::Official));
    }

    #[test]
    fn decision_serializes() {
        let d = AclDecision::deny_required("needs PROTECTED", Clearance::Protected);
        let s = serde_json::to_string(&d).unwrap();
        assert!(s.contains("\"decision\":\"deny\""));
        assert!(s.contains("\"required_clearance\":\"protected\""));
    }
}
