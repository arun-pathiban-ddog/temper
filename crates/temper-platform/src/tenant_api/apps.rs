use axum::Json;
use axum::extract::{Extension, Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::IntoResponse;
use temper_authz::AuthenticatedRequestContext;

use super::auth::{
    PlatformResourceAuthorization, require_authenticated, require_resource_authorization,
    require_same_tenant,
};
use super::authorization_error;
use crate::state::PlatformState;

/// List the credential tenant's authorized application catalog.
pub(crate) async fn list_os_apps(
    State(state): State<PlatformState>,
    authenticated: Option<Extension<AuthenticatedRequestContext>>,
) -> impl IntoResponse {
    let authenticated = match require_authenticated(authenticated.as_deref()) {
        Ok(authenticated) => authenticated,
        Err(status) => return authorization_error(status),
    };
    if let Err(status) = require_resource_authorization(
        &state,
        authenticated,
        PlatformResourceAuthorization {
            action: "read_app_catalog",
            resource_type: "AppCatalog",
            resource_id: "all",
            attrs: std::collections::BTreeMap::new(),
        },
    ) {
        return authorization_error(status);
    }
    let apps = crate::os_apps::list_os_apps();
    (StatusCode::OK, Json(serde_json::json!({ "apps": apps })))
}

/// Return one authorized application guide.
pub(crate) async fn get_os_app_guide(
    State(state): State<PlatformState>,
    authenticated: Option<Extension<AuthenticatedRequestContext>>,
    Path(name): Path<String>,
) -> impl IntoResponse {
    let authenticated = match require_authenticated(authenticated.as_deref()) {
        Ok(authenticated) => authenticated,
        Err(status) => return authorization_error(status),
    };
    if let Err(status) = require_resource_authorization(
        &state,
        authenticated,
        PlatformResourceAuthorization {
            action: "read_app_catalog",
            resource_type: "AppCatalogEntry",
            resource_id: &name,
            attrs: std::collections::BTreeMap::new(),
        },
    ) {
        return authorization_error(status);
    }
    match crate::os_apps::get_app_guide(&name) {
        Some(guide) => (
            StatusCode::OK,
            Json(serde_json::json!({"name": name, "guide": guide})),
        ),
        None => (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({
                "error": format!("No app guide found for '{name}'"),
            })),
        ),
    }
}

/// Return follow-latest state only for the credential-bound tenant.
pub(crate) async fn list_genesis_follow_updates(
    State(state): State<PlatformState>,
    authenticated: Option<Extension<AuthenticatedRequestContext>>,
) -> impl IntoResponse {
    let authenticated = match require_authenticated(authenticated.as_deref()) {
        Ok(authenticated) => authenticated,
        Err(status) => return authorization_error(status),
    };
    let tenant = authenticated.tenant().as_str();
    if let Err(status) = require_resource_authorization(
        &state,
        authenticated,
        PlatformResourceAuthorization {
            action: "read_app_installs",
            resource_type: "AppInstall",
            resource_id: tenant,
            attrs: std::collections::BTreeMap::new(),
        },
    ) {
        return authorization_error(status);
    }
    let updates = crate::genesis_install::list_genesis_follow_latest_updates(&state)
        .await
        .into_iter()
        .filter(|update| update.tenant == tenant)
        .collect::<Vec<_>>();
    (
        StatusCode::OK,
        Json(serde_json::json!({ "value": updates })),
    )
}

/// Install one pinned application into the credential-bound tenant.
pub(crate) async fn install_genesis_app(
    State(state): State<PlatformState>,
    authenticated: Option<Extension<AuthenticatedRequestContext>>,
    Json(req): Json<crate::genesis_install::GenesisRegistryInstallRequest>,
) -> impl IntoResponse {
    let authenticated = match require_authenticated(authenticated.as_deref()) {
        Ok(authenticated) => authenticated,
        Err(status) => return authorization_error(status),
    };
    if let Err(status) = require_same_tenant(authenticated, &req.tenant).and_then(|_| {
        require_resource_authorization(
            &state,
            authenticated,
            PlatformResourceAuthorization {
                action: "install_app",
                resource_type: "App",
                resource_id: &req.app_ref,
                attrs: std::collections::BTreeMap::from([
                    (
                        "targetTenant".to_string(),
                        serde_json::Value::String(req.tenant.clone()),
                    ),
                    (
                        "registryTenant".to_string(),
                        serde_json::Value::String(req.registry_tenant.clone()),
                    ),
                ]),
            },
        )
    }) {
        return authorization_error(status);
    }
    match crate::genesis_install::install_genesis_app_from_registry(&state, req).await {
        Ok(result) => (StatusCode::OK, Json(serde_json::json!(result))),
        Err(error) if error.contains("not found") => (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({ "error": error })),
        ),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({ "error": error })),
        ),
    }
}

/// Export one pinned application bundle from the credential-bound registry tenant.
pub(crate) async fn get_genesis_app_bundle(
    State(state): State<PlatformState>,
    authenticated: Option<Extension<AuthenticatedRequestContext>>,
    headers: HeaderMap,
    Path((owner, name, hash)): Path<(String, String, String)>,
) -> impl IntoResponse {
    // A public repository's bundle is the same content Genesis already serves to
    // anonymous callers over `git clone`, just encoded differently, so requiring
    // a credential here and not there was inconsistent — and it made installing
    // a public app impossible, because the install client sends no credential at
    // all (it sends only `X-Tenant-Id`). Naming a tenant cannot escalate
    // anything on this path: the only rows it can reach are ones already
    // world-readable over git.
    let Some(authenticated) = authenticated.as_deref() else {
        let tenant = headers
            .get("x-tenant-id")
            .and_then(|value| value.to_str().ok())
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .unwrap_or("default")
            .to_string();
        return match anonymous_public_bundle(&state, &tenant, &owner, &name, &hash).await {
            Ok(response) => response,
            Err(status) => authorization_error(status),
        };
    };
    let registry_tenant = authenticated.tenant().as_str();
    let resource_id = format!("{owner}/{name}@{hash}");
    if let Err(status) = require_resource_authorization(
        &state,
        authenticated,
        PlatformResourceAuthorization {
            action: "read_app_bundle",
            resource_type: "App",
            resource_id: &resource_id,
            attrs: std::collections::BTreeMap::from([
                (
                    "owner".to_string(),
                    serde_json::Value::String(owner.clone()),
                ),
                ("name".to_string(), serde_json::Value::String(name.clone())),
                (
                    "versionHash".to_string(),
                    serde_json::Value::String(hash.clone()),
                ),
            ]),
        },
    ) {
        return authorization_error(status);
    }
    match crate::genesis_install::export_genesis_registry_bundle(
        &state,
        registry_tenant,
        &owner,
        &name,
        &hash,
    )
    .await
    {
        Ok(bundle) => (StatusCode::OK, Json(serde_json::json!(bundle))),
        Err(error) if error.contains("not found") => (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({ "error": error })),
        ),
        Err(error) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({ "error": error })),
        ),
    }
}

/// Serve a bundle without a credential, only when the backing repository is
/// public. Anything else is refused as unauthenticated.
async fn anonymous_public_bundle(
    state: &PlatformState,
    tenant: &str,
    owner: &str,
    name: &str,
    hash: &str,
) -> Result<(StatusCode, Json<serde_json::Value>), StatusCode> {
    let tenant_id = super::auth::validate_tenant_id(tenant)?;
    let repository_id = format!("rp-{owner}-{name}");
    if !state
        .server
        .ensure_entity_loaded(&tenant_id, "Repository", &repository_id)
        .await
    {
        return Err(StatusCode::UNAUTHORIZED);
    }
    let repository = state
        .server
        .get_tenant_entity_state(&tenant_id, "Repository", &repository_id)
        .await
        .map_err(|_| StatusCode::UNAUTHORIZED)?;
    let visibility = repository
        .state
        .fields
        .get("Visibility")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default();
    if visibility != "public" {
        tracing::warn!(
            tenant,
            repository_id,
            visibility,
            "anonymous Genesis bundle read refused: repository is not public"
        );
        return Err(StatusCode::UNAUTHORIZED);
    }
    match crate::genesis_install::export_genesis_registry_bundle(state, tenant, owner, name, hash)
        .await
    {
        Ok(bundle) => Ok((StatusCode::OK, Json(serde_json::json!(bundle)))),
        Err(error) if error.contains("not found") => Ok((
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({ "error": error })),
        )),
        Err(error) => Ok((
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({ "error": error })),
        )),
    }
}
