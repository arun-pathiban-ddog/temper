//! Explicit, bounded construction of external inference context.

use serde_json::Value;

pub(super) fn validate(template: &Value) -> Result<(), String> {
    if !super::context_type(template) {
        return Err("system_one state must resolve from text, object, or array".into());
    }
    visit(template, None, None, 0, &mut 0).map(|_| ())
}

pub(super) fn resolve(template: &Value, entity: &Value, params: &Value) -> Result<Value, String> {
    let value = visit(template, Some(entity), Some(params), 0, &mut 0)?;
    if !super::context_type(&value) {
        return Err("resolved system_one state must be text, object, or array".into());
    }
    Ok(value)
}

fn visit(
    value: &Value,
    entity: Option<&Value>,
    params: Option<&Value>,
    depth: usize,
    nodes: &mut usize,
) -> Result<Value, String> {
    *nodes += 1;
    if depth > 32 || *nodes > 16_384 {
        return Err("system_one state exceeds nesting or node budget".into());
    }
    match value {
        Value::Object(object) if object.contains_key("ref") => {
            if object.len() != 1 {
                return Err("system_one ref binding cannot contain other fields".into());
            }
            let reference = object["ref"]
                .as_str()
                .ok_or("system_one ref must be a string")?;
            let (source, field) = reference
                .split_once('.')
                .ok_or("expected entity.Field or params.Param reference")?;
            if !super::identifier(field) || !matches!(source, "entity" | "params") {
                return Err(format!(
                    "unsupported system_one state reference '{reference}'"
                ));
            }
            let snapshot = if source == "entity" { entity } else { params };
            match snapshot {
                None => Ok(value.clone()),
                Some(snapshot) => snapshot
                    .get(field)
                    .cloned()
                    .ok_or_else(|| format!("missing system_one state reference '{reference}'")),
            }
        }
        Value::Object(object) => {
            let resolved = object
                .iter()
                .map(|(k, v)| Ok((k.clone(), visit(v, entity, params, depth + 1, nodes)?)))
                .collect::<Result<serde_json::Map<_, _>, String>>()?;
            Ok(Value::Object(resolved))
        }
        Value::Array(array) => array
            .iter()
            .map(|v| visit(v, entity, params, depth + 1, nodes))
            .collect::<Result<Vec<_>, _>>()
            .map(Value::Array),
        _ => Ok(value.clone()),
    }
}

pub(super) fn references(template: &Value) -> Vec<(String, String)> {
    let mut references = Vec::new();
    let mut pending = vec![template];
    while let Some(value) = pending.pop() {
        match value {
            Value::Object(object) if object.contains_key("ref") => {
                // Callers validate the binding tree before collecting names.
                let reference = object["ref"].as_str().expect("validated reference string");
                let (source, field) = reference
                    .split_once('.')
                    .expect("validated reference source");
                references.push((source.to_string(), field.to_string()));
            }
            Value::Object(object) => pending.extend(object.values()),
            Value::Array(array) => pending.extend(array),
            _ => {}
        }
    }
    references
}
