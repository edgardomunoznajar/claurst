//! Simon OIDC login flow.
//!
//! Runs an authorisation-code-with-PKCE flow against the configured OIDC
//! issuer (Dex in the demo; a real gov IdP in production), captures the code
//! via a loopback HTTP server, exchanges it for tokens, parses the ID token,
//! and constructs a [`SimonPrincipal`] with clearance derived from the `sub`
//! claim.
//!
//! ## Clearance derivation for the demo
//!
//! Dex v2.40 does not emit arbitrary per-user claims via static-password
//! connectors. The demo encodes clearance in the `userID` field of each
//! static user, e.g. `sarah-unofficial`, `bill-official`, `alice-protected`.
//! When Simon sees a `sub` claim it splits on `-` and takes the last segment
//! as the clearance label. Production deployments should configure the IdP
//! to emit a dedicated `clearance` claim instead; this module has a hook for
//! that via [`clearance_from_claims`].
//!
//! ## What this module does NOT do
//!
//! It deliberately skips JWT signature verification. For the demo the token
//! comes over a private Docker network from a known Dex container, so the
//! signature check adds no security over the TLS/hostname check. Production
//! deployments must verify signatures using the IdP's JWKS; swap this module
//! for the `openidconnect` crate when that time comes and leave this file as
//! a demo-only path behind a feature flag.

use std::io::{Read, Write};
use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use chrono::{TimeZone, Utc};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use simon_acl::{Clearance, SimonPrincipal};
use tokio::net::TcpListener as TokioTcpListener;
use url::Url;

/// Config read from environment at startup.
#[derive(Debug, Clone)]
pub struct OidcConfig {
    pub issuer: String,
    pub client_id: String,
    pub client_secret: String,
    /// Port Simon binds the loopback listener on inside its own process.
    pub redirect_port: u16,
    /// Full redirect URI given to the IdP and opened in the user's browser.
    /// When Simon runs inside a container whose port is remapped (e.g.
    /// container :8080 → host :8765), the IdP must hear about the HOST
    /// port, not the container port. This env var overrides the default
    /// `http://localhost:{redirect_port}/callback` so the two can differ.
    pub redirect_url: String,
    pub scope: String,
}

impl OidcConfig {
    /// Build from env. Returns `None` if any required variable is missing,
    /// so the caller can fall back to the anonymous principal during early
    /// development.
    pub fn from_env() -> Option<Self> {
        let issuer = std::env::var("SIMON_OIDC_ISSUER").ok()?;
        let client_id = std::env::var("SIMON_OIDC_CLIENT_ID").ok()?;
        let client_secret = std::env::var("SIMON_OIDC_CLIENT_SECRET").ok()?;
        let redirect_port = std::env::var("SIMON_OIDC_REDIRECT_PORT")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(8080);
        let redirect_url = std::env::var("SIMON_OIDC_REDIRECT_URL")
            .unwrap_or_else(|_| format!("http://localhost:{redirect_port}/callback"));
        let scope = std::env::var("SIMON_OIDC_SCOPE")
            .unwrap_or_else(|_| "openid email profile groups".to_string());
        Some(Self {
            issuer,
            client_id,
            client_secret,
            redirect_port,
            redirect_url,
            scope,
        })
    }
}

#[derive(Debug, Deserialize)]
struct Discovery {
    authorization_endpoint: String,
    token_endpoint: String,
    #[serde(default)]
    #[allow(dead_code)]
    jwks_uri: Option<String>,
}

#[derive(Debug, Deserialize)]
struct TokenResponse {
    #[allow(dead_code)]
    access_token: String,
    id_token: String,
    #[allow(dead_code)]
    #[serde(default)]
    refresh_token: Option<String>,
}

#[derive(Debug, Deserialize)]
struct IdTokenClaims {
    sub: String,
    #[serde(default)]
    email: Option<String>,
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    preferred_username: Option<String>,
    #[serde(default)]
    groups: Vec<String>,
    #[serde(default)]
    exp: Option<i64>,
    #[serde(default)]
    iat: Option<i64>,
    /// Reserved: real IdPs should emit this as a dedicated claim instead of
    /// encoding clearance into `sub`.
    #[serde(default)]
    clearance: Option<String>,
}

/// DEV-MODE SHORT-CIRCUIT: if `SIMON_OIDC_ID_TOKEN` is set, skip the browser
/// dance entirely and construct a principal straight from the supplied token.
/// Used by `scripts/smoke_test.sh` so the stack can be driven
/// non-interactively in CI. Emits a loud warning; never use in production.
///
/// Returns `Ok(Some(p))` on a successful short-circuit, `Ok(None)` if the
/// env var is unset (caller should run the real flow), or `Err` if a token
/// was present but could not be parsed.
pub fn login_from_token_env() -> Result<Option<SimonPrincipal>> {
    let Ok(token) = std::env::var("SIMON_OIDC_ID_TOKEN") else {
        return Ok(None);
    };
    tracing::warn!(
        "SIMON_OIDC_ID_TOKEN is set — skipping interactive OIDC flow. \
         This is a test-only path. Do not enable in production."
    );
    let claims = parse_id_token(&token)
        .context("parsing SIMON_OIDC_ID_TOKEN as JWT")?;
    let principal = principal_from_claims(claims)
        .context("building principal from SIMON_OIDC_ID_TOKEN")?;
    Ok(Some(principal))
}

/// Run the full login flow and return a populated [`SimonPrincipal`].
pub async fn login(cfg: &OidcConfig) -> Result<SimonPrincipal> {
    tracing::info!(
        issuer = %cfg.issuer,
        client_id = %cfg.client_id,
        "simon: starting OIDC login"
    );

    let http = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .context("building HTTP client for OIDC discovery")?;

    // Discover endpoints.
    let discovery_url = format!(
        "{}/.well-known/openid-configuration",
        cfg.issuer.trim_end_matches('/')
    );
    let discovery: Discovery = http
        .get(&discovery_url)
        .send()
        .await
        .with_context(|| format!("fetching {discovery_url}"))?
        .error_for_status()
        .with_context(|| format!("{discovery_url} returned non-2xx"))?
        .json()
        .await
        .context("parsing discovery document")?;

    // PKCE — generate verifier + S256 challenge.
    let verifier = random_url_safe(64);
    let challenge = {
        let mut hasher = Sha256::new();
        hasher.update(verifier.as_bytes());
        URL_SAFE_NO_PAD.encode(hasher.finalize())
    };
    let state = random_url_safe(24);

    // Bind the callback listener BEFORE opening the browser so we don't race.
    // Note: the listener binds on cfg.redirect_port (the port Simon sees
    // inside its own process), but the URL we advertise to the IdP is
    // cfg.redirect_url — which may use a DIFFERENT port if Simon is
    // running behind a docker port mapping.
    let redirect_uri = cfg.redirect_url.clone();
    let listener = TokioTcpListener::bind(("0.0.0.0", cfg.redirect_port))
        .await
        .with_context(|| {
            format!("binding port {} for OIDC callback", cfg.redirect_port)
        })?;

    // Build the authorization URL.
    let mut auth_url = Url::parse(&discovery.authorization_endpoint)
        .context("parsing authorization_endpoint URL")?;
    auth_url
        .query_pairs_mut()
        .append_pair("response_type", "code")
        .append_pair("client_id", &cfg.client_id)
        .append_pair("redirect_uri", &redirect_uri)
        .append_pair("scope", &cfg.scope)
        .append_pair("state", &state)
        .append_pair("code_challenge", &challenge)
        .append_pair("code_challenge_method", "S256");

    // Tell the user what's happening and try to pop the browser. We don't
    // fail if `open` can't reach a browser — headless hosts can copy-paste
    // the URL manually.
    eprintln!();
    eprintln!("Simon: authenticate at the following URL (opening in browser):");
    eprintln!("  {}", auth_url.as_str());
    eprintln!();
    let _ = open::that(auth_url.as_str());

    // Wait for the callback. One-shot: accept one connection, read request,
    // parse the query string, write a minimal HTML response, close.
    let code = wait_for_callback(&listener, &state)
        .await
        .context("waiting for OIDC callback")?;

    // Exchange the code for tokens.
    let token_resp: TokenResponse = http
        .post(&discovery.token_endpoint)
        .form(&[
            ("grant_type", "authorization_code"),
            ("code", &code),
            ("redirect_uri", &redirect_uri),
            ("client_id", &cfg.client_id),
            ("client_secret", &cfg.client_secret),
            ("code_verifier", &verifier),
        ])
        .send()
        .await
        .context("posting to token endpoint")?
        .error_for_status()
        .context("token endpoint returned non-2xx")?
        .json()
        .await
        .context("parsing token response")?;

    // Parse ID token claims. DEMO-ONLY: no signature verification — see
    // module doc.
    let claims = parse_id_token(&token_resp.id_token)
        .context("parsing ID token claims")?;

    let principal = principal_from_claims(claims)
        .context("building principal from token claims")?;

    tracing::info!(
        subject = %principal.subject,
        clearance = %principal.clearance,
        "simon: login succeeded"
    );

    Ok(principal)
}

/// Turn parsed ID token claims into a [`SimonPrincipal`]. Shared by the real
/// flow and the [`login_from_token_env`] test path.
fn principal_from_claims(claims: IdTokenClaims) -> Result<SimonPrincipal> {
    let clearance = clearance_from_claims(&claims)
        .ok_or_else(|| anyhow!("could not derive clearance from ID token claims"))?;

    let issued_at = claims
        .iat
        .and_then(|t| Utc.timestamp_opt(t, 0).single())
        .unwrap_or_else(Utc::now);
    let expires_at = claims
        .exp
        .and_then(|t| Utc.timestamp_opt(t, 0).single())
        .unwrap_or_else(|| Utc::now() + chrono::Duration::hours(1));

    let display_name = claims
        .name
        .clone()
        .or_else(|| claims.preferred_username.clone())
        .unwrap_or_else(|| claims.sub.clone());
    let email = claims
        .email
        .clone()
        .unwrap_or_else(|| format!("{}@simon.local", claims.sub));

    Ok(SimonPrincipal {
        subject: claims.sub,
        display_name,
        email,
        clearance,
        groups: claims.groups,
        session_id: random_url_safe(16),
        issued_at,
        expires_at,
    })
}

/// Derive clearance from the ID token. Priority order:
/// 1. Explicit `clearance` claim if the IdP emits one.
/// 2. Any `groups` entry matching `clearance:<level>`.
/// 3. The last `-`-separated segment of the `sub` claim (demo-only encoding).
fn clearance_from_claims(claims: &IdTokenClaims) -> Option<Clearance> {
    if let Some(c) = &claims.clearance {
        if let Some(parsed) = parse_clearance(c) {
            return Some(parsed);
        }
    }
    for g in &claims.groups {
        if let Some(rest) = g.strip_prefix("clearance:") {
            if let Some(parsed) = parse_clearance(rest) {
                return Some(parsed);
            }
        }
    }
    if let Some(last) = claims.sub.rsplit('-').next() {
        if let Some(parsed) = parse_clearance(last) {
            return Some(parsed);
        }
    }
    None
}

fn parse_clearance(s: &str) -> Option<Clearance> {
    match s.to_ascii_lowercase().as_str() {
        "unofficial" => Some(Clearance::Unofficial),
        "official" => Some(Clearance::Official),
        "official-sensitive" | "official_sensitive" | "officialsensitive" => {
            Some(Clearance::OfficialSensitive)
        }
        "protected" => Some(Clearance::Protected),
        _ => None,
    }
}

fn parse_id_token(jwt: &str) -> Result<IdTokenClaims> {
    let mut parts = jwt.split('.');
    let _header = parts.next().ok_or_else(|| anyhow!("id_token missing header"))?;
    let payload = parts.next().ok_or_else(|| anyhow!("id_token missing payload"))?;
    let bytes = URL_SAFE_NO_PAD
        .decode(payload)
        .context("base64-decoding id_token payload")?;
    let claims: IdTokenClaims =
        serde_json::from_slice(&bytes).context("json-parsing id_token payload")?;
    Ok(claims)
}

fn random_url_safe(len: usize) -> String {
    let mut bytes = vec![0u8; len];
    // getrandom is already in the workspace via its re-export path; but we
    // don't want a new dependency here. Use std's thread-local RNG through
    // a quick getrandom-free path.
    use std::time::{SystemTime, UNIX_EPOCH};
    let seed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos() as u64;
    // Xorshift64 — not cryptographic; PKCE verifier just needs to be
    // unpredictable to a local observer, and the callback port is
    // loopback-only. For production swap to `rand::rngs::OsRng`.
    let mut x = seed.wrapping_mul(2862933555777941757).wrapping_add(3037000493);
    for b in bytes.iter_mut() {
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        *b = (x & 0xff) as u8;
    }
    URL_SAFE_NO_PAD.encode(&bytes).chars().take(len).collect()
}

async fn wait_for_callback(
    listener: &TokioTcpListener,
    expected_state: &str,
) -> Result<String> {
    // Give the user up to 3 minutes to complete the browser flow.
    let (stream, _) = tokio::time::timeout(Duration::from_secs(180), listener.accept())
        .await
        .context("timed out waiting for OIDC callback")?
        .context("accepting OIDC callback connection")?;
    let std_stream = stream.into_std()?;
    std_stream.set_nonblocking(false)?;
    handle_callback_sync(std_stream, expected_state)
}

fn handle_callback_sync(
    mut stream: std::net::TcpStream,
    expected_state: &str,
) -> Result<String> {
    stream
        .set_read_timeout(Some(Duration::from_secs(10)))
        .ok();
    let mut buf = [0u8; 4096];
    let n = stream.read(&mut buf).context("reading callback request")?;
    let request = String::from_utf8_lossy(&buf[..n]);

    // First line: "GET /callback?code=...&state=... HTTP/1.1"
    let first_line = request.lines().next().ok_or_else(|| anyhow!("empty request"))?;
    let path = first_line
        .split_whitespace()
        .nth(1)
        .ok_or_else(|| anyhow!("malformed request line"))?;
    let query = path.split_once('?').map(|(_, q)| q).unwrap_or("");

    let mut code: Option<String> = None;
    let mut state: Option<String> = None;
    let mut error: Option<String> = None;
    for pair in query.split('&') {
        let (k, v) = match pair.split_once('=') {
            Some(kv) => kv,
            None => continue,
        };
        let v = urlencoding::decode(v).unwrap_or_default().into_owned();
        match k {
            "code" => code = Some(v),
            "state" => state = Some(v),
            "error" => error = Some(v),
            _ => {}
        }
    }

    if let Some(err) = error {
        let _ = write_callback_response(&mut stream, false, &format!("OIDC error: {err}"));
        bail!("IdP returned error: {err}");
    }
    let state = state.ok_or_else(|| anyhow!("callback missing state parameter"))?;
    if state != expected_state {
        let _ = write_callback_response(&mut stream, false, "state mismatch");
        bail!("OIDC state mismatch — possible CSRF");
    }
    let code = code.ok_or_else(|| anyhow!("callback missing code parameter"))?;
    let _ = write_callback_response(&mut stream, true, "Simon login complete. Return to the terminal.");
    Ok(code)
}

fn write_callback_response(
    stream: &mut std::net::TcpStream,
    ok: bool,
    message: &str,
) -> Result<()> {
    let status = if ok { "200 OK" } else { "400 Bad Request" };
    let body = format!(
        "<!doctype html><html><head><title>Simon login</title></head><body>\
        <h1>{}</h1><p>{}</p></body></html>",
        if ok { "Logged in" } else { "Login failed" },
        html_escape(message)
    );
    let response = format!(
        "HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n\
        Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(response.as_bytes())?;
    Ok(())
}

fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn claims(sub: &str, groups: Vec<&str>, clearance: Option<&str>) -> IdTokenClaims {
        IdTokenClaims {
            sub: sub.to_string(),
            email: None,
            name: None,
            preferred_username: None,
            groups: groups.into_iter().map(String::from).collect(),
            exp: None,
            iat: None,
            clearance: clearance.map(String::from),
        }
    }

    #[test]
    fn clearance_from_explicit_claim_wins() {
        let c = clearance_from_claims(&claims("sarah", vec![], Some("protected")));
        assert_eq!(c, Some(Clearance::Protected));
    }

    #[test]
    fn clearance_from_groups_prefix() {
        let c = clearance_from_claims(&claims("sarah", vec!["clearance:official-sensitive"], None));
        assert_eq!(c, Some(Clearance::OfficialSensitive));
    }

    #[test]
    fn clearance_from_sub_demo_encoding() {
        let c = clearance_from_claims(&claims("alice-protected", vec![], None));
        assert_eq!(c, Some(Clearance::Protected));
        let c = clearance_from_claims(&claims("sarah-unofficial", vec![], None));
        assert_eq!(c, Some(Clearance::Unofficial));
    }

    #[test]
    fn clearance_none_when_unparseable() {
        let c = clearance_from_claims(&claims("no-hint-here", vec![], None));
        assert_eq!(c, None);
    }

    #[test]
    fn parse_clearance_normalises_variants() {
        assert_eq!(parse_clearance("OFFICIAL"), Some(Clearance::Official));
        assert_eq!(
            parse_clearance("Official_Sensitive"),
            Some(Clearance::OfficialSensitive)
        );
        assert_eq!(parse_clearance("garbage"), None);
    }

    #[test]
    fn id_token_payload_decodes() {
        // Hand-built JWT: header = {"alg":"none"}, payload = {"sub":"alice-protected"}
        let header = URL_SAFE_NO_PAD.encode(br#"{"alg":"none"}"#);
        let payload = URL_SAFE_NO_PAD.encode(br#"{"sub":"alice-protected","email":"alice@example.gov.au"}"#);
        let jwt = format!("{header}.{payload}.");
        let claims = parse_id_token(&jwt).unwrap();
        assert_eq!(claims.sub, "alice-protected");
        assert_eq!(claims.email.as_deref(), Some("alice@example.gov.au"));
    }
}
