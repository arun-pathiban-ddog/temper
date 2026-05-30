// TemperFS WASM Module Authorization Policies

// Allow blob_adapter to make HTTP calls (blob storage upload/download)
permit(
    principal is Agent,
    action == Action::"http_call",
    resource is HttpEndpoint
) when {
    context.module == "blob_adapter"
};

// Allow blob_adapter to access secrets (blob_endpoint, blob_bucket)
permit(
    principal is Agent,
    action == Action::"access_secret",
    resource is Secret
) when {
    context.module == "blob_adapter"
};

// ADR-0099: preserve read access under platform read-authz. Reads were ungated
// before read-authz landed; this broad read/list permit keeps them open so
// enabling default-deny read enforcement does not break existing read callers.
permit(
  principal,
  action in [Action::"read", Action::"list"],
  resource is HttpEndpoint
);
