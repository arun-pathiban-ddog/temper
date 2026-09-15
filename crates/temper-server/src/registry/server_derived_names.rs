//! Naming collisions between an app's CSDL and the kernel's own fields.

use super::CsdlDocument;

/// Report CSDL properties that collide with server-derived field names.
///
/// `Id`, `id`, `Status`, `status`, `has_spec` and `HasSpec` are owned by the
/// kernel: the actor overwrites them with the entity id and the state-machine
/// state on every hydrate (`canonicalize_entity_field_map`). An app is free to
/// *declare* one in its CSDL — nothing rejects it — but the value it stores is
/// then destroyed on the actor path while the OData projection still reports
/// the declared property, so the same field reads differently depending on
/// which surface you came in through.
///
/// That divergence is silent, and it cost a full day: Genesis declares `Id` on
/// its git objects to hold the bare sha, and the kernel's own bundle lookup
/// compared that field against a sha, got the entity id back, and reported
/// intact commits as "not found" — with nothing anywhere saying why.
///
/// This warns rather than rejects on purpose. Existing apps (Genesis among
/// them) already declare these names, so failing registration would take them
/// down; the fix is to name the collision loudly and let apps migrate.
pub(super) fn warn_on_server_derived_csdl_properties(tenant: &str, csdl: &CsdlDocument) {
    for schema in &csdl.schemas {
        for entity_type in &schema.entity_types {
            for property in &entity_type.properties {
                if temper_spec::automaton::is_server_derived_field_name(&property.name) {
                    tracing::warn!(
                        tenant,
                        namespace = %schema.namespace,
                        entity = %entity_type.name,
                        property = %property.name,
                        "CSDL declares a server-derived field name; the actor will \
                         overwrite it, so this property reads differently over OData \
                         than through the entity actor"
                    );
                }
            }
        }
    }
}
