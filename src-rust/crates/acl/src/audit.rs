//! Audit sink for denied (and optionally allowed) ACL decisions.
//!
//! Every Deny must be logged so a reviewer can prove, after the fact, that the
//! harness stopped a forbidden access. Logs are append-only JSONL written to a
//! sink injected at startup.

use std::sync::Arc;

use async_trait::async_trait;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::{AclDecision, Operation, ResourceRef, SimonPrincipal};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuditEvent {
    pub at: DateTime<Utc>,
    pub session_id: String,
    pub subject: String,
    pub email: String,
    pub clearance: String,
    pub operation: Operation,
    pub resource: ResourceRef,
    pub decision: AclDecision,
    pub enforcer: String,
}

impl AuditEvent {
    pub fn new(
        principal: &SimonPrincipal,
        resource: &ResourceRef,
        op: Operation,
        decision: AclDecision,
        enforcer: &'static str,
    ) -> Self {
        Self {
            at: Utc::now(),
            session_id: principal.session_id.clone(),
            subject: principal.subject.clone(),
            email: principal.email.clone(),
            clearance: principal.clearance.label().to_string(),
            operation: op,
            resource: resource.clone(),
            decision,
            enforcer: enforcer.to_string(),
        }
    }
}

#[async_trait]
pub trait AuditSink: Send + Sync + 'static {
    async fn write(&self, event: AuditEvent);
}

pub type SharedAuditSink = Arc<dyn AuditSink>;

/// Sink that drops events on the floor. Useful for tests.
pub struct NullSink;

#[async_trait]
impl AuditSink for NullSink {
    async fn write(&self, _event: AuditEvent) {}
}

/// Sink that writes JSONL to tracing at INFO level. No file handle — use this
/// when tracing is already piped to your log collector.
pub struct TracingSink;

#[async_trait]
impl AuditSink for TracingSink {
    async fn write(&self, event: AuditEvent) {
        if let Ok(line) = serde_json::to_string(&event) {
            tracing::info!(target: "simon_acl::audit", "{line}");
        }
    }
}
