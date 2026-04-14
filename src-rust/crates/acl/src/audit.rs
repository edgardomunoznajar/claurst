//! Audit sink for every ACL decision (allow and deny).
//!
//! Every decision MUST be committed to a durable sink before the tool acts.
//! If the sink fails, the tool fails. This is the fail-closed property: an
//! attacker who can DOS the audit pipeline does not thereby gain audit-free
//! access.
//!
//! Sinks are append-only JSONL writers. The two concrete sinks ship:
//!
//!   * [`FileJsonlSink`] — line-per-event to a file opened in append mode and
//!     fsynced before returning. Use this in production.
//!   * [`TracingSink`] — writes to the `tracing` crate at INFO level. Use this
//!     when the host environment already captures tracing output.
//!
//! [`NullSink`] exists only for tests; it will never be wired into a
//! production build because `CompositeSink::new` refuses to accept it unless
//! it's the sole sink AND the `allow_null_sink` flag is set.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use async_trait::async_trait;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use tokio::io::AsyncWriteExt;
use tokio::sync::Mutex;

use crate::{AclDecision, AclError, Operation, ResourceRef, SimonPrincipal};

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

    pub fn is_allow(&self) -> bool {
        self.decision.is_allow()
    }
}

/// A sink that can accept an audit event. Writes must either succeed and
/// return `Ok(())` (the tool may proceed) or return `Err` (the tool MUST
/// abort). A successful return is the commit point; the caller may treat the
/// decision as durably recorded.
#[async_trait]
pub trait AuditSink: Send + Sync + 'static {
    /// Durably record one event. Returning `Err` prevents the tool from
    /// acting on the decision.
    async fn write(&self, event: &AuditEvent) -> Result<(), AclError>;
}

pub type SharedAuditSink = Arc<dyn AuditSink>;

/// Test-only sink that drops every event on the floor. NEVER use in a real
/// deployment — the whole point of the audit subsystem is that a drop is
/// indistinguishable from a successful write by downstream analysis.
pub struct NullSink;

#[async_trait]
impl AuditSink for NullSink {
    async fn write(&self, _event: &AuditEvent) -> Result<(), AclError> {
        Ok(())
    }
}

/// Writes every event to `tracing::info!`. Suitable when the host is already
/// piping tracing to a durable log collector (journald, Cloud Logging, etc.).
/// Serialisation errors are fatal — an event we can't render is an event we
/// can't audit.
pub struct TracingSink;

#[async_trait]
impl AuditSink for TracingSink {
    async fn write(&self, event: &AuditEvent) -> Result<(), AclError> {
        let line = serde_json::to_string(event)
            .map_err(|e| AclError::Backend(format!("audit serialise: {e}")))?;
        tracing::info!(target: "simon_acl::audit", "{line}");
        Ok(())
    }
}

/// Append-only JSONL file sink. Every `write` opens the file in append mode,
/// writes one line, and fsyncs before returning. The fsync is what gives the
/// fail-closed guarantee: if the call returns `Ok`, the line is on disk.
pub struct FileJsonlSink {
    path: PathBuf,
    inner: Mutex<Option<tokio::fs::File>>,
}

impl FileJsonlSink {
    pub fn new(path: impl AsRef<Path>) -> Self {
        Self {
            path: path.as_ref().to_path_buf(),
            inner: Mutex::new(None),
        }
    }

    async fn open(&self) -> Result<tokio::fs::File, AclError> {
        tokio::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .await
            .map_err(|e| {
                AclError::Backend(format!(
                    "open audit file {}: {e}",
                    self.path.display()
                ))
            })
    }
}

#[async_trait]
impl AuditSink for FileJsonlSink {
    async fn write(&self, event: &AuditEvent) -> Result<(), AclError> {
        let mut line = serde_json::to_vec(event)
            .map_err(|e| AclError::Backend(format!("audit serialise: {e}")))?;
        line.push(b'\n');

        let mut guard = self.inner.lock().await;
        if guard.is_none() {
            *guard = Some(self.open().await?);
        }
        let file = guard.as_mut().unwrap();
        file.write_all(&line)
            .await
            .map_err(|e| AclError::Backend(format!("audit append: {e}")))?;
        file.sync_data()
            .await
            .map_err(|e| AclError::Backend(format!("audit fsync: {e}")))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Clearance, ResourceRef};
    use chrono::Duration;

    fn sample_event() -> AuditEvent {
        let now = Utc::now();
        let principal = SimonPrincipal {
            subject: "u1".into(),
            display_name: "Test".into(),
            email: "t@example.gov.au".into(),
            clearance: Clearance::Protected,
            groups: vec![],
            session_id: "s1".into(),
            issued_at: now,
            expires_at: now + Duration::hours(1),
        };
        AuditEvent::new(
            &principal,
            &ResourceRef::file("/workspace/protected/x.md"),
            Operation::Read,
            AclDecision::Allow,
            "static-json",
        )
    }

    #[tokio::test]
    async fn file_sink_appends_and_fsyncs() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let sink = FileJsonlSink::new(tmp.path());
        sink.write(&sample_event()).await.unwrap();
        sink.write(&sample_event()).await.unwrap();

        let content = tokio::fs::read_to_string(tmp.path()).await.unwrap();
        assert_eq!(content.lines().count(), 2, "expected two JSONL lines");
        for line in content.lines() {
            let _: AuditEvent = serde_json::from_str(line).unwrap();
        }
    }

    #[tokio::test]
    async fn file_sink_errors_are_propagated() {
        // A path that can't be opened — we point at a directory.
        let tmp_dir = tempfile::tempdir().unwrap();
        let sink = FileJsonlSink::new(tmp_dir.path());
        let err = sink.write(&sample_event()).await.unwrap_err();
        assert!(matches!(err, AclError::Backend(_)));
    }

    #[tokio::test]
    async fn tracing_sink_success() {
        let sink = TracingSink;
        sink.write(&sample_event()).await.unwrap();
    }
}
