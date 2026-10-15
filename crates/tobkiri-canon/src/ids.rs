//! Validated identifiers shared by every runtime layer without crate cycles.

use crate::{AllocationError, CanonError, Digest};
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use std::{fmt, str::FromStr};

fn kebab(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.as_bytes()[0].is_ascii_lowercase()
        && !value.ends_with('-')
        && !value.contains("--")
        && value
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
}
fn pack(value: &str) -> bool {
    let Some((publisher, name)) = value.split_once('.') else {
        return false;
    };
    kebab(publisher) && kebab(name)
}
fn token(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 255
        && value.as_bytes()[0].is_ascii_alphanumeric()
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
        && !value.contains("..")
}
fn shape(value: &str) -> bool {
    let mut count = 0;
    value.len() <= 128
        && value.split('.').all(|part| {
            count += 1;
            kebab(part)
        })
        && (3..=10).contains(&count)
}
fn component(value: &str) -> bool {
    if ["CON", "PRN", "AUX", "NUL"]
        .iter()
        .any(|reserved| value.eq_ignore_ascii_case(reserved))
        || (value.len() == 4
            && (value.get(..3).is_some_and(|prefix| {
                prefix.eq_ignore_ascii_case("COM") || prefix.eq_ignore_ascii_case("LPT")
            }))
            && matches!(value.as_bytes()[3], b'1'..=b'9'))
    {
        return false;
    }
    !value.is_empty()
        && value.len() <= 128
        && value.as_bytes()[0].is_ascii_alphanumeric()
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

fn copy_precharged(value: &str) -> Result<String, AllocationError> {
    let mut copied = String::new();
    copied
        .try_reserve_exact(value.len())
        .map_err(|_| AllocationError::Allocator)?;
    if copied.capacity() != value.len() {
        return Err(AllocationError::Unavailable);
    }
    copied.push_str(value);
    Ok(copied)
}

macro_rules! identifier {
    ($name:ident, $validate:ident) => {
        #[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
        pub struct $name(String);
        impl $name {
            pub fn new(value: impl Into<String>) -> Result<Self, CanonError> {
                let value = value.into();
                if !$validate(&value) {
                    return Err(CanonError::Invalid(format!(
                        "invalid {}: {value}",
                        stringify!($name)
                    )));
                }
                Ok(Self(value))
            }
            pub fn parse(value: &str) -> Result<Self, CanonError> {
                Self::new(value)
            }
            pub fn as_str(&self) -> &str {
                &self.0
            }
            /// New owned UTF-8 bytes required for an exact fallible clone.
            pub fn clone_storage_bytes(&self) -> usize {
                self.0.len()
            }
            /// Copy an already validated ID only AFTER a trusted caller has
            /// prepaid clone_storage_bytes(); performs no validation allocation.
            pub fn try_clone_precharged(&self) -> Result<Self, AllocationError> {
                Ok(Self(copy_precharged(&self.0)?))
            }
            /// Validate without allocating, then copy only AFTER the trusted
            /// caller has prepaid value.len(). No permission is created here.
            pub fn try_parse_precharged(value: &str) -> Result<Self, AllocationError> {
                if !$validate(value) {
                    return Err(AllocationError::Invalid);
                }
                Ok(Self(copy_precharged(value)?))
            }
        }
        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(self.as_str())
            }
        }
        impl FromStr for $name {
            type Err = CanonError;
            fn from_str(v: &str) -> Result<Self, Self::Err> {
                Self::parse(v)
            }
        }
        impl Serialize for $name {
            fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
                s.serialize_str(self.as_str())
            }
        }
        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
                Self::parse(&String::deserialize(d)?).map_err(serde::de::Error::custom)
            }
        }
    };
}
identifier!(PackId, pack);
identifier!(ProfileId, component);
identifier!(ShapeName, shape);
identifier!(OpName, kebab);
identifier!(RequestId, token);
identifier!(UiSessionId, token);
identifier!(InputSourceId, token);

impl PackId {
    pub fn publisher(&self) -> &str {
        self.as_str().split_once('.').expect("validated PackId").0
    }
    pub fn name(&self) -> &str {
        self.as_str().split_once('.').expect("validated PackId").1
    }
}
impl ShapeName {
    pub fn owner(&self) -> PackId {
        PackId::parse(
            &self
                .as_str()
                .split('.')
                .take(2)
                .collect::<Vec<_>>()
                .join("."),
        )
        .expect("validated shape owner")
    }
    /// Authoritative dotted suffix, never reconstructed from a WIT name.
    pub fn path(&self) -> &str {
        self.as_str()
            .splitn(3, '.')
            .nth(2)
            .expect("validated ShapeName")
    }
    /// Flatten only for the WIT interface mapping; registry lookup is required
    /// in the reverse direction because `ai.provider` and `ai-provider` collide.
    pub fn interface(&self) -> String {
        self.path().replace('.', "-")
    }
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct PackHash(Digest);
impl PackHash {
    pub fn new(digest: Digest) -> Self {
        Self(digest)
    }
    pub fn parse(value: &str) -> Result<Self, CanonError> {
        Ok(Self(Digest::parse(value)?))
    }
    pub fn as_str(&self) -> &str {
        self.0.as_str()
    }
    pub fn digest(&self) -> &Digest {
        &self.0
    }
    pub fn hex(&self) -> &str {
        self.0.hex()
    }
    pub fn clone_storage_bytes(&self) -> usize {
        0
    }
    pub fn try_clone_precharged(&self) -> Result<Self, AllocationError> {
        Ok(Self(self.0.clone()))
    }
}
impl fmt::Display for PackHash {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(f)
    }
}
impl FromStr for PackHash {
    type Err = CanonError;
    fn from_str(v: &str) -> Result<Self, Self::Err> {
        Self::parse(v)
    }
}
impl From<Digest> for PackHash {
    fn from(v: Digest) -> Self {
        Self(v)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct ShapeVersion {
    pub major: u16,
    pub minor: u16,
}
impl ShapeVersion {
    pub fn new(major: u16, minor: u16) -> Self {
        Self { major, minor }
    }
    pub fn parse(value: &str) -> Result<Self, CanonError> {
        let Some((major, minor)) = value.split_once('.') else {
            return Err(CanonError::Invalid(
                "shape version must be major.minor".into(),
            ));
        };
        let number = |v: &str| -> Result<u16, CanonError> {
            if v.is_empty()
                || v.len() > 1 && v.starts_with('0')
                || !v.bytes().all(|b| b.is_ascii_digit())
            {
                return Err(CanonError::Invalid("invalid shape version".into()));
            }
            v.parse()
                .map_err(|_| CanonError::Invalid("shape version exceeds u16".into()))
        };
        Ok(Self {
            major: number(major)?,
            minor: number(minor)?,
        })
    }
}
impl fmt::Display for ShapeVersion {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}.{}", self.major, self.minor)
    }
}
impl FromStr for ShapeVersion {
    type Err = CanonError;
    fn from_str(v: &str) -> Result<Self, Self::Err> {
        Self::parse(v)
    }
}
impl Serialize for ShapeVersion {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&self.to_string())
    }
}
impl<'de> Deserialize<'de> for ShapeVersion {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        Self::parse(&String::deserialize(d)?).map_err(serde::de::Error::custom)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct ShapeRef {
    pub name: ShapeName,
    pub version: ShapeVersion,
}
impl ShapeRef {
    pub fn new(name: ShapeName, version: ShapeVersion) -> Self {
        Self { name, version }
    }
    pub fn parse(value: &str) -> Result<Self, CanonError> {
        let Some((name, version)) = value.split_once('@') else {
            return Err(CanonError::Invalid(
                "shape reference needs @major.minor".into(),
            ));
        };
        Ok(Self {
            name: ShapeName::parse(name)?,
            version: ShapeVersion::parse(version)?,
        })
    }
    pub fn clone_storage_bytes(&self) -> usize {
        self.name.clone_storage_bytes()
    }
    pub fn try_clone_precharged(&self) -> Result<Self, AllocationError> {
        Ok(Self {
            name: self.name.try_clone_precharged()?,
            version: self.version,
        })
    }
}
impl fmt::Display for ShapeRef {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}@{}", self.name, self.version)
    }
}
impl FromStr for ShapeRef {
    type Err = CanonError;
    fn from_str(v: &str) -> Result<Self, Self::Err> {
        Self::parse(v)
    }
}
impl Serialize for ShapeRef {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&self.to_string())
    }
}
impl<'de> Deserialize<'de> for ShapeRef {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        Self::parse(&String::deserialize(d)?).map_err(serde::de::Error::custom)
    }
}
