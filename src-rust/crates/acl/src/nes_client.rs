//! HTTP client for the NES mock (and, eventually, the real NQRY enterprise
//! search ACL endpoint).
//!
//! The NES service exposes two endpoints Simon cares about:
//!
//!   POST /acl/check
//!     { principal: SimonPrincipal, resource: ResourceRef, operation: Operation }
//!     -> AclDecision
//!
//!   POST /acl/filter
//!     { principal, candidates: [ResourceRef] }
//!     -> { visible: [ResourceRef] }
//!
//! For the demo, the mock service is a single FastAPI container backed by a
//! JSON file. In production, this client would point at a real NES deployment.

use async_trait::async_trait;
use serde::{Deserialize, Serialize};

use crate::{
    AclDecision, AclEnforcer, AclError, Operation, ResourceRef, SimonPrincipal,
};

pub struct NesHttpEnforcer {
    base_url: String,
    http: reqwest::Client,
}

impl NesHttpEnforcer {
    pub fn new(base_url: impl Into<String>) -> Result<Self, AclError> {
        let http = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(5))
            .build()
            .map_err(|e| AclError::Backend(format!("http client: {e}")))?;
        Ok(Self {
            base_url: base_url.into().trim_end_matches('/').to_string(),
            http,
        })
    }
}

#[derive(Serialize)]
struct CheckRequest<'a> {
    principal: &'a SimonPrincipal,
    resource: &'a ResourceRef,
    operation: Operation,
}

#[derive(Serialize)]
struct FilterRequest<'a> {
    principal: &'a SimonPrincipal,
    candidates: &'a [ResourceRef],
}

#[derive(Deserialize)]
struct FilterResponse {
    visible: Vec<ResourceRef>,
}

#[async_trait]
impl AclEnforcer for NesHttpEnforcer {
    async fn check(
        &self,
        principal: &SimonPrincipal,
        resource: &ResourceRef,
        op: Operation,
    ) -> Result<AclDecision, AclError> {
        let url = format!("{}/acl/check", self.base_url);
        let body = CheckRequest {
            principal,
            resource,
            operation: op,
        };
        let resp = self
            .http
            .post(&url)
            .json(&body)
            .send()
            .await
            .map_err(|e| AclError::Backend(format!("nes request: {e}")))?;
        if !resp.status().is_success() {
            // Fail closed: a broken policy service must NEVER be treated as
            // permissive. A prompt-injected attacker who DOSes NES must not
            // thereby unlock data.
            return Ok(AclDecision::deny(format!(
                "nes service returned {}",
                resp.status()
            )));
        }
        resp.json::<AclDecision>()
            .await
            .map_err(|e| AclError::Backend(format!("nes decode: {e}")))
    }

    async fn filter_visible(
        &self,
        principal: &SimonPrincipal,
        candidates: Vec<ResourceRef>,
    ) -> Result<Vec<ResourceRef>, AclError> {
        let url = format!("{}/acl/filter", self.base_url);
        let body = FilterRequest {
            principal,
            candidates: &candidates,
        };
        let resp = self
            .http
            .post(&url)
            .json(&body)
            .send()
            .await
            .map_err(|e| AclError::Backend(format!("nes filter: {e}")))?;
        if !resp.status().is_success() {
            // Fail closed: drop everything if we can't confirm visibility.
            return Ok(Vec::new());
        }
        let parsed: FilterResponse = resp
            .json()
            .await
            .map_err(|e| AclError::Backend(format!("nes filter decode: {e}")))?;
        Ok(parsed.visible)
    }

    fn backend_name(&self) -> &'static str {
        "nes-http"
    }
}
