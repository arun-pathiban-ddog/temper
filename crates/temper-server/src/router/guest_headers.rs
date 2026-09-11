//! Which inbound headers a WASM guest is allowed to see.
//!
//! One audited home for the credential filter, testable against its real
//! output, so a future guest-context constructor reuses it rather than
//! reimplementing it.

use axum::http::HeaderMap;

/// Request headers that carry an ambient caller credential and must never be
/// forwarded into a WASM guest's invocation context.
///
/// A guest runs untrusted module code and can read everything it is handed, so any
/// credential here is one it could replay — against this server, an upstream, or an
/// identity-aware proxy fronting us. The list therefore covers direct credentials,
/// the forwarded-auth family injected by such proxies, and cloud instance tokens
/// (ARN-208).
///
/// This is the *inbound* guard (host → guest). The separate *outbound* sanitizer
/// for guest-supplied headers on internal re-entry lives in
/// `temper_wasm::host_trait::internal_http::internal_header_allowed`, and the two
/// lists are deliberately **not** the same set: that one also drops routing and
/// tenant-identity headers (`host`, `forwarded`, `x-forwarded-for`, `x-tenant-id`,
/// the `x-temper-*` authority namespace) which are legitimate context on the way
/// in. Adding a credential header here does not imply an edit there.
pub(crate) const GUEST_FORBIDDEN_CREDENTIAL_HEADERS: [&str; 16] = [
    "authorization",
    "proxy-authorization",
    "cookie",
    "x-api-key",
    // Forwarded-auth family: whatever an identity-aware proxy injects once it has
    // authenticated the caller. None of these are used by the current deployment;
    // they are stripped defensively so putting Temper behind such a proxy never
    // silently starts handing live IdP tokens to guest modules.
    "x-forwarded-authorization",
    "x-forwarded-access-token",
    // Google IAP
    "x-goog-iap-jwt-assertion",
    // Cloudflare Access
    "cf-access-jwt-assertion",
    // AWS ALB `authenticate-oidc`
    "x-amzn-oidc-accesstoken",
    "x-amzn-oidc-data",
    "x-amzn-oidc-identity",
    // Azure App Service EasyAuth
    "x-ms-token-aad-access-token",
    "x-ms-token-aad-id-token",
    "x-ms-token-aad-refresh-token",
    // oauth2-proxy `--set-xauthrequest`
    "x-auth-request-access-token",
    // Cloud instance credential.
    "x-amz-security-token",
];

/// Whether a request header carries an ambient caller credential and must not be
/// forwarded into a WASM guest's invocation context.
pub(crate) fn is_credential_header(name: &str) -> bool {
    GUEST_FORBIDDEN_CREDENTIAL_HEADERS
        .iter()
        .any(|candidate| name.eq_ignore_ascii_case(candidate))
}

/// Build the header list a WASM guest sees for an inbound HTTP dispatch.
///
/// Today this is the only place inbound headers cross into guest-visible state, so
/// the credential filter lives here rather than inline at the call site: one audited
/// home, and testable against its real output. `pub(crate)` so any future
/// guest-context constructor reuses it instead of reimplementing the filter.
///
/// Note this covers *headers* only. The request path handed to the guest includes
/// the query string (deliberately — git needs `service=`), so a credential passed as
/// `?access_token=…` is a different carrier of the same invariant; tracked with the
/// outbound mirror in ARN-346 (ARN-208).
pub(crate) fn guest_visible_headers(
    headers: &HeaderMap,
    forwards_credential: bool,
) -> Vec<(String, String)> {
    headers
        .iter()
        .filter(|(name, _)| {
            // `forwards_credential` is an exception for exactly one header: the
            // protocol credential the guest is required to resolve itself. It is
            // not a general opt-out of the denylist. Cookies, `x-api-key` and the
            // forwarded-auth family (IAP, Cloudflare Access, ALB OIDC) stay
            // stripped on these routes too — a git module needs the GitToken the
            // client presented, never the caller's session cookie or whatever an
            // identity-aware proxy injected in front of the kernel.
            if forwards_credential && name.as_str().eq_ignore_ascii_case("authorization") {
                return true;
            }
            !is_credential_header(name.as_str())
        })
        .filter_map(|(k, v)| {
            v.to_str()
                .ok()
                .map(|s| (k.as_str().to_string(), s.to_string()))
        })
        .collect()
}
