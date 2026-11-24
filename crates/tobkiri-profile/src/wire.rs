use crate::*;
use serde::{Deserializer, Serializer, ser::SerializeMap};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct StepWire {
    id: Option<String>,
    role: Option<ShapeRef>,
    #[serde(default)]
    overlay: OverlayAccess,
    out: Option<String>,
    stream: Option<StreamRoute>,
    #[serde(default, deserialize_with = "present_kind")]
    call: Option<PortCall>,
    #[serde(default, deserialize_with = "present_kind")]
    flow: Option<SubFlow>,
    #[serde(default, deserialize_with = "present_kind")]
    branch: Option<Branch>,
    #[serde(default, deserialize_with = "present_kind")]
    parallel: Option<Parallel>,
    #[serde(rename = "loop", default, deserialize_with = "present_kind")]
    loop_body: Option<Loop>,
    #[serde(rename = "return", default, deserialize_with = "present")]
    return_value: Option<CanonValue>,
    #[serde(default, deserialize_with = "present_kind")]
    next: Option<Next>,
}
fn present<'de, D: Deserializer<'de>>(d: D) -> Result<Option<CanonValue>, D::Error> {
    Ok(Some(CanonValue::deserialize(d)?))
}
fn present_kind<'de, D: Deserializer<'de>, T: Deserialize<'de>>(
    d: D,
) -> Result<Option<T>, D::Error> {
    Ok(Some(T::deserialize(d)?))
}
impl<'de> Deserialize<'de> for Step {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let value = StepWire::deserialize(d)?;
        let mut kinds = Vec::new();
        if let Some(v) = value.call {
            kinds.push(StepKind::Call(v));
        }
        if let Some(v) = value.flow {
            kinds.push(StepKind::Flow(v));
        }
        if let Some(v) = value.branch {
            kinds.push(StepKind::Branch(v));
        }
        if let Some(v) = value.parallel {
            kinds.push(StepKind::Parallel(v));
        }
        if let Some(v) = value.loop_body {
            kinds.push(StepKind::Loop(v));
        }
        if let Some(v) = value.return_value {
            kinds.push(StepKind::Return(v));
        }
        if let Some(v) = value.next {
            kinds.push(StepKind::Next(v));
        }
        if kinds.len() != 1 {
            return Err(serde::de::Error::custom(
                "each step requires exactly one step-kind field",
            ));
        }
        Ok(Self {
            id: value.id,
            role: value.role,
            overlay: value.overlay,
            kind: kinds.remove(0),
            out: value.out,
            stream: value.stream,
        })
    }
}
impl Serialize for Step {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let mut map = s.serialize_map(None)?;
        if let Some(v) = &self.id {
            map.serialize_entry("id", v)?;
        }
        if let Some(v) = &self.role {
            map.serialize_entry("role", v)?;
        }
        map.serialize_entry("overlay", &self.overlay)?;
        if let Some(v) = &self.out {
            map.serialize_entry("out", v)?;
        }
        if let Some(v) = &self.stream {
            map.serialize_entry("stream", v)?;
        }
        match &self.kind {
            StepKind::Call(v) => map.serialize_entry("call", v)?,
            StepKind::Flow(v) => map.serialize_entry("flow", v)?,
            StepKind::Branch(v) => map.serialize_entry("branch", v)?,
            StepKind::Parallel(v) => map.serialize_entry("parallel", v)?,
            StepKind::Loop(v) => map.serialize_entry("loop", v)?,
            StepKind::Return(v) => map.serialize_entry("return", v)?,
            StepKind::Next(v) => map.serialize_entry("next", v)?,
        }
        map.end()
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TargetWire {
    #[serde(default, deserialize_with = "present_kind")]
    role: Option<ShapeRef>,
    #[serde(default, deserialize_with = "present_kind")]
    flow: Option<String>,
    #[serde(default, deserialize_with = "present_kind")]
    step: Option<String>,
}
impl<'de> Deserialize<'de> for OverlayTarget {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let value = TargetWire::deserialize(d)?;
        match (value.role, value.flow, value.step) {
            (Some(role), None, None) => Ok(Self::Role { role }),
            (None, Some(flow), Some(step)) => Ok(Self::Step { flow, step }),
            _ => Err(serde::de::Error::custom(
                "overlay target must contain role or exactly flow and step",
            )),
        }
    }
}
impl Serialize for OverlayTarget {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let mut map = s.serialize_map(None)?;
        match self {
            Self::Role { role } => map.serialize_entry("role", role)?,
            Self::Step { flow, step } => {
                map.serialize_entry("flow", flow)?;
                map.serialize_entry("step", step)?;
            }
        }
        map.end()
    }
}
impl Step {
    pub fn new(id: impl Into<String>, kind: StepKind) -> Self {
        Self {
            id: Some(id.into()),
            role: None,
            overlay: OverlayAccess::Closed,
            kind,
            out: None,
            stream: None,
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct InputEntryWire {
    source_pack: PackId,
    exact_input_view: ShapeRef,
    operation: OpName,
}
impl<'de> Deserialize<'de> for InputEntry {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct ExactEntry;
        impl<'de> serde::de::Visitor<'de> for ExactEntry {
            type Value = InputEntry;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("an exact input record {source_pack, exact_input_view, operation}")
            }
            fn visit_map<M: serde::de::MapAccess<'de>>(
                self,
                map: M,
            ) -> Result<InputEntry, M::Error> {
                let value =
                    InputEntryWire::deserialize(serde::de::value::MapAccessDeserializer::new(map))?;
                Ok(InputEntry {
                    source_pack: value.source_pack,
                    exact_input_view: value.exact_input_view,
                    operation: value.operation,
                })
            }
            fn visit_str<E: serde::de::Error>(self, _: &str) -> Result<InputEntry, E> {
                Err(E::custom(
                    "Pack-ID-only startup.inputs entries are unsupported; select source_pack, exact_input_view and operation explicitly",
                ))
            }
        }
        d.deserialize_any(ExactEntry)
    }
}
