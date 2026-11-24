//! Serde adapters for typed new-format documents; canonical codecs never use Serde parsing.

use crate::CanonValue;
use serde::{
    Deserialize, Deserializer, Serialize, Serializer,
    de::{MapAccess, SeqAccess, Visitor},
    ser::{SerializeMap, SerializeSeq},
};
use std::{collections::BTreeMap, fmt};

impl Serialize for CanonValue {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self {
            Self::Null => serializer.serialize_unit(),
            Self::Bool(value) => serializer.serialize_bool(*value),
            Self::Int(value) => serializer.serialize_i64(*value),
            Self::BigInt(_) => Err(serde::ser::Error::custom(
                "use the lossless canonical encoder for legacy large integers",
            )),
            Self::Float(value) if value.is_finite() => serializer.serialize_f64(*value),
            Self::Float(_) => Err(serde::ser::Error::custom(
                "nonfinite value requires the explicit LegacyPackSig encoder",
            )),
            Self::Str(value) => serializer.serialize_str(value),
            Self::Arr(values) => {
                let mut seq = serializer.serialize_seq(Some(values.len()))?;
                for value in values {
                    seq.serialize_element(value)?;
                }
                seq.end()
            }
            Self::Obj(values) => {
                let mut map = serializer.serialize_map(Some(values.len()))?;
                for (key, value) in values {
                    map.serialize_entry(key, value)?;
                }
                map.end()
            }
        }
    }
}
impl<'de> Deserialize<'de> for CanonValue {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct ValueVisitor;
        impl<'de> Visitor<'de> for ValueVisitor {
            type Value = CanonValue;
            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("a JSON value")
            }
            fn visit_unit<E: serde::de::Error>(self) -> Result<Self::Value, E> {
                Ok(CanonValue::Null)
            }
            fn visit_none<E: serde::de::Error>(self) -> Result<Self::Value, E> {
                Ok(CanonValue::Null)
            }
            fn visit_bool<E: serde::de::Error>(self, v: bool) -> Result<Self::Value, E> {
                Ok(CanonValue::Bool(v))
            }
            fn visit_i64<E: serde::de::Error>(self, v: i64) -> Result<Self::Value, E> {
                Ok(CanonValue::Int(v))
            }
            fn visit_u64<E: serde::de::Error>(self, v: u64) -> Result<Self::Value, E> {
                Ok(i64::try_from(v)
                    .map_or_else(|_| CanonValue::BigInt(v.to_string()), CanonValue::Int))
            }
            fn visit_f64<E: serde::de::Error>(self, v: f64) -> Result<Self::Value, E> {
                Ok(CanonValue::Float(v))
            }
            fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<Self::Value, E> {
                Ok(CanonValue::Str(v.into()))
            }
            fn visit_string<E: serde::de::Error>(self, v: String) -> Result<Self::Value, E> {
                Ok(CanonValue::Str(v))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut a: A) -> Result<Self::Value, A::Error> {
                let mut values = Vec::new();
                while let Some(value) = a.next_element()? {
                    values.push(value);
                }
                Ok(CanonValue::Arr(values))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut a: A) -> Result<Self::Value, A::Error> {
                let mut values = BTreeMap::new();
                while let Some((key, value)) = a.next_entry::<String, CanonValue>()? {
                    if values.contains_key(&key) {
                        return Err(serde::de::Error::custom("duplicate JSON key"));
                    }
                    values.insert(key, value);
                }
                Ok(CanonValue::Obj(values))
            }
        }
        deserializer.deserialize_any(ValueVisitor)
    }
}
impl CanonValue {
    pub fn as_str(&self) -> Option<&str> {
        if let Self::Str(value) = self {
            Some(value)
        } else {
            None
        }
    }
    pub fn as_i64(&self) -> Option<i64> {
        if let Self::Int(value) = self {
            Some(*value)
        } else {
            None
        }
    }
    pub fn as_object(&self) -> Option<&BTreeMap<String, Self>> {
        if let Self::Obj(value) = self {
            Some(value)
        } else {
            None
        }
    }
    pub fn as_array(&self) -> Option<&[Self]> {
        if let Self::Arr(value) = self {
            Some(value)
        } else {
            None
        }
    }
    pub fn get(&self, name: &str) -> Option<&Self> {
        self.as_object()?.get(name)
    }
}
impl From<String> for CanonValue {
    fn from(value: String) -> Self {
        Self::Str(value)
    }
}
impl From<&str> for CanonValue {
    fn from(value: &str) -> Self {
        Self::Str(value.into())
    }
}
impl From<i64> for CanonValue {
    fn from(value: i64) -> Self {
        Self::Int(value)
    }
}
impl From<bool> for CanonValue {
    fn from(value: bool) -> Self {
        Self::Bool(value)
    }
}
