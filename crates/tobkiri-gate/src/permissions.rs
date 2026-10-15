use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

/// Execution limits, always intersected by taking the smaller value.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ResourceLimits {
    pub fuel_per_call: u64,
    pub memory_mib: u64,
    pub wall_ms: u64,
    #[serde(default = "default_table_elements")]
    pub table_elements: u32,
    #[serde(default = "default_output_bytes")]
    pub output_bytes: u64,
}
fn default_table_elements() -> u32 {
    16384
}
fn default_output_bytes() -> u64 {
    4 * 1024 * 1024
}
impl ResourceLimits {
    /// No resource restriction. This conveys no effect permissions.
    pub const fn unlimited() -> Self {
        Self {
            fuel_per_call: u64::MAX,
            memory_mib: u64::MAX,
            wall_ms: u64::MAX,
            table_elements: u32::MAX,
            output_bytes: u64::MAX,
        }
    }
    pub fn intersect(&self, other: &Self) -> Self {
        Self {
            fuel_per_call: self.fuel_per_call.min(other.fuel_per_call),
            memory_mib: self.memory_mib.min(other.memory_mib),
            wall_ms: self.wall_ms.min(other.wall_ms),
            table_elements: self.table_elements.min(other.table_elements),
            output_bytes: self.output_bytes.min(other.output_bytes),
        }
    }
    pub fn is_subset_of(&self, other: &Self) -> bool {
        self.fuel_per_call <= other.fuel_per_call
            && self.memory_mib <= other.memory_mib
            && self.wall_ms <= other.wall_ms
            && self.table_elements <= other.table_elements
            && self.output_bytes <= other.output_bytes
    }
}

/// Required default bounds for every Flow loop.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LoopLimits {
    pub max_iterations: u64,
    pub max_wall_ms: u64,
    pub max_cost_units: u64,
}
impl LoopLimits {
    pub fn validate(&self) -> bool {
        self.max_iterations > 0 && self.max_wall_ms > 0 && self.max_cost_units > 0
    }
    pub fn intersect(&self, other: &Self) -> Self {
        Self {
            max_iterations: self.max_iterations.min(other.max_iterations),
            max_wall_ms: self.max_wall_ms.min(other.max_wall_ms),
            max_cost_units: self.max_cost_units.min(other.max_cost_units),
        }
    }
    pub fn is_subset_of(&self, other: &Self) -> bool {
        self.max_iterations <= other.max_iterations
            && self.max_wall_ms <= other.max_wall_ms
            && self.max_cost_units <= other.max_cost_units
    }
}

/// A filesystem permission applies to a trusted, core-issued root ID.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FsPermission {
    pub root: String,
    pub read: bool,
    pub write: bool,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct NetworkPermissions {
    pub origins: BTreeSet<String>,
    pub user_origins: Option<UserOrigins>,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UserOrigins {
    pub max: u32,
    pub allow_loopback: bool,
}

/// Secret names may be used opaquely; plaintext requires a separate reveal bit.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SecretPermission {
    pub name: String,
    #[serde(default)]
    pub reveal: bool,
    #[serde(default)]
    pub optional: bool,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct StoragePermissions {
    pub private: bool,
    pub public_read: bool,
    pub public_write: bool,
}

/// Normalized effect permissions, independent of Profile and manifest parsers.
/// Absence denies. Network origins and command/root IDs are exact matches.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct PermissionSet {
    pub network: NetworkPermissions,
    pub fs: Vec<FsPermission>,
    pub proc: BTreeSet<String>,
    pub pty: bool,
    pub secrets: Vec<SecretPermission>,
    pub storage: StoragePermissions,
    pub native: bool,
    pub cross_profile_storage: bool,
    pub audit_read: bool,
    pub audit_append: bool,
    pub host_io: BTreeSet<String>,
    /// Fully qualified consumed port shape refs authorized by core binding checks.
    pub ports: BTreeSet<String>,
    pub mediator: bool,
    #[serde(alias = "limits")]
    pub wasm: ResourceLimits,
    pub loops: LoopLimits,
}

fn intersection(a: &BTreeSet<String>, b: &BTreeSet<String>) -> BTreeSet<String> {
    a.intersection(b).cloned().collect()
}
impl PermissionSet {
    /// The meet of two permission sets; it can never widen either side.
    pub fn intersect(&self, other: &Self) -> Self {
        let fs = self
            .fs
            .iter()
            .filter_map(|a| {
                other
                    .fs
                    .iter()
                    .find(|b| a.root == b.root)
                    .map(|b| FsPermission {
                        root: a.root.clone(),
                        read: a.read && b.read,
                        write: a.write && b.write,
                    })
            })
            .filter(|f| f.read || f.write)
            .collect();
        let secrets = self
            .secrets
            .iter()
            .filter_map(|a| {
                other
                    .secrets
                    .iter()
                    .find(|b| a.name == b.name)
                    .map(|b| SecretPermission {
                        name: a.name.clone(),
                        reveal: a.reveal && b.reveal,
                        optional: a.optional && b.optional,
                    })
            })
            .collect();
        Self {
            network: NetworkPermissions {
                origins: intersection(&self.network.origins, &other.network.origins),
                user_origins: self
                    .network
                    .user_origins
                    .as_ref()
                    .zip(other.network.user_origins.as_ref())
                    .map(|(a, b)| UserOrigins {
                        max: a.max.min(b.max),
                        allow_loopback: a.allow_loopback && b.allow_loopback,
                    }),
            },
            fs,
            proc: intersection(&self.proc, &other.proc),
            pty: self.pty && other.pty,
            secrets,
            storage: StoragePermissions {
                private: self.storage.private && other.storage.private,
                public_read: self.storage.public_read && other.storage.public_read,
                public_write: self.storage.public_write && other.storage.public_write,
            },
            native: self.native && other.native,
            cross_profile_storage: self.cross_profile_storage && other.cross_profile_storage,
            audit_read: self.audit_read && other.audit_read,
            audit_append: self.audit_append && other.audit_append,
            host_io: intersection(&self.host_io, &other.host_io),
            ports: intersection(&self.ports, &other.ports),
            mediator: self.mediator && other.mediator,
            wasm: self.wasm.intersect(&other.wasm),
            loops: self.loops.intersect(&other.loops),
        }
    }
    pub fn is_subset_of(&self, other: &Self) -> bool {
        self.network.origins.is_subset(&other.network.origins)
            && self.network.user_origins.as_ref().is_none_or(|a| {
                other
                    .network
                    .user_origins
                    .as_ref()
                    .is_some_and(|b| a.max <= b.max && (!a.allow_loopback || b.allow_loopback))
            })
            && self.fs.iter().all(|a| {
                other
                    .fs
                    .iter()
                    .any(|b| a.root == b.root && (!a.read || b.read) && (!a.write || b.write))
            })
            && self.proc.is_subset(&other.proc)
            && (!self.pty || other.pty)
            && self.secrets.iter().all(|a| {
                other
                    .secrets
                    .iter()
                    .any(|b| a.name == b.name && (!a.reveal || b.reveal))
            })
            && (!self.storage.private || other.storage.private)
            && (!self.storage.public_read || other.storage.public_read)
            && (!self.storage.public_write || other.storage.public_write)
            && (!self.native || other.native)
            && (!self.cross_profile_storage || other.cross_profile_storage)
            && (!self.audit_read || other.audit_read)
            && (!self.audit_append || other.audit_append)
            && self.host_io.is_subset(&other.host_io)
            && self.ports.is_subset(&other.ports)
            && (!self.mediator || other.mediator)
            && self.wasm.is_subset_of(&other.wasm)
            && self.loops.is_subset_of(&other.loops)
    }
    /// Only added authority is returned; revocations do not require a prompt.
    pub fn additions(&self, old: &Self) -> Self {
        let mut result = self.clone();
        result.network.origins = self
            .network
            .origins
            .difference(&old.network.origins)
            .cloned()
            .collect();
        result.network.user_origins = self
            .network
            .user_origins
            .as_ref()
            .filter(|a| {
                !old.network
                    .user_origins
                    .as_ref()
                    .is_some_and(|b| a.max <= b.max && (!a.allow_loopback || b.allow_loopback))
            })
            .cloned();
        result.fs = self
            .fs
            .iter()
            .filter_map(|a| {
                let b = old.fs.iter().find(|b| a.root == b.root);
                let read = a.read && !b.is_some_and(|b| b.read);
                let write = a.write && !b.is_some_and(|b| b.write);
                (read || write).then(|| FsPermission {
                    root: a.root.clone(),
                    read,
                    write,
                })
            })
            .collect();
        result.secrets = self
            .secrets
            .iter()
            .filter_map(|a| {
                let b = old.secrets.iter().find(|b| a.name == b.name);
                (b.is_none() || (a.reveal && !b.is_some_and(|b| b.reveal))).then(|| {
                    SecretPermission {
                        name: a.name.clone(),
                        reveal: a.reveal && !b.is_some_and(|b| b.reveal),
                        optional: a.optional,
                    }
                })
            })
            .collect();
        result.proc = self.proc.difference(&old.proc).cloned().collect();
        result.ports = self.ports.difference(&old.ports).cloned().collect();
        result.host_io = self.host_io.difference(&old.host_io).cloned().collect();
        result.pty &= !old.pty;
        result.native &= !old.native;
        result.cross_profile_storage &= !old.cross_profile_storage;
        result.audit_read &= !old.audit_read;
        result.audit_append &= !old.audit_append;
        result.mediator &= !old.mediator;
        result.storage.private &= !old.storage.private;
        result.storage.public_read &= !old.storage.public_read;
        result.storage.public_write &= !old.storage.public_write;
        result.wasm = ResourceLimits {
            fuel_per_call: if self.wasm.fuel_per_call > old.wasm.fuel_per_call {
                self.wasm.fuel_per_call
            } else {
                0
            },
            memory_mib: if self.wasm.memory_mib > old.wasm.memory_mib {
                self.wasm.memory_mib
            } else {
                0
            },
            wall_ms: if self.wasm.wall_ms > old.wasm.wall_ms {
                self.wasm.wall_ms
            } else {
                0
            },
            table_elements: if self.wasm.table_elements > old.wasm.table_elements {
                self.wasm.table_elements
            } else {
                0
            },
            output_bytes: if self.wasm.output_bytes > old.wasm.output_bytes {
                self.wasm.output_bytes
            } else {
                0
            },
        };
        result.loops = LoopLimits {
            max_iterations: if self.loops.max_iterations > old.loops.max_iterations {
                self.loops.max_iterations
            } else {
                0
            },
            max_wall_ms: if self.loops.max_wall_ms > old.loops.max_wall_ms {
                self.loops.max_wall_ms
            } else {
                0
            },
            max_cost_units: if self.loops.max_cost_units > old.loops.max_cost_units {
                self.loops.max_cost_units
            } else {
                0
            },
        };
        result
    }
    pub fn has_additions(&self) -> bool {
        self != &Self::default()
    }
}
/// Added permissions between two revisions (new minus old).
pub fn lattice_diff(old: &PermissionSet, new: &PermissionSet) -> PermissionSet {
    new.additions(old)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn four_way_intersection_cannot_add_an_origin_or_dangerous_permission() {
        let mut declared = PermissionSet::default();
        declared
            .network
            .origins
            .extend(["https://one.example".into(), "https://two.example".into()]);
        declared.native = true;
        declared.cross_profile_storage = true;
        declared.wasm = ResourceLimits::unlimited();
        let mut ceiling = declared.clone();
        ceiling.network.origins.remove("https://two.example");
        ceiling.native = false;
        ceiling.wasm.memory_mib = 64;
        let mut pack = declared.clone();
        pack.wasm.memory_mib = 32;
        let mut approval = declared.clone();
        approval.cross_profile_storage = false;
        let effective = declared
            .intersect(&ceiling)
            .intersect(&pack)
            .intersect(&approval);
        assert!(effective.is_subset_of(&declared));
        assert!(effective.is_subset_of(&ceiling));
        assert!(effective.is_subset_of(&pack));
        assert!(effective.is_subset_of(&approval));
        assert_eq!(effective.network.origins.len(), 1);
        assert!(!effective.native);
        assert!(!effective.cross_profile_storage);
        assert_eq!(effective.wasm.memory_mib, 32);
    }
    #[test]
    fn update_diff_contains_only_added_authority() {
        let mut old = PermissionSet::default();
        old.network.origins.insert("https://old.example".into());
        old.fs.push(FsPermission {
            root: "project".into(),
            read: true,
            write: false,
        });
        old.secrets.push(SecretPermission {
            name: "api-key".into(),
            reveal: false,
            optional: true,
        });
        old.wasm.memory_mib = 64;
        let mut new = old.clone();
        new.network.origins.clear();
        new.network.origins.insert("https://new.example".into());
        new.fs[0].write = true;
        new.secrets[0].reveal = true;
        new.wasm.memory_mib = 32;
        let additions = lattice_diff(&old, &new);
        assert_eq!(
            additions.network.origins,
            BTreeSet::from(["https://new.example".into()])
        );
        assert!(!additions.fs[0].read);
        assert!(additions.fs[0].write);
        assert!(additions.secrets[0].reveal);
        assert_eq!(additions.wasm.memory_mib, 0);
        assert!(!lattice_diff(&new, &new).has_additions());
        assert!(!lattice_diff(&old, &PermissionSet::default()).has_additions());
    }
    #[test]
    fn unknown_serialized_permissions_fail_closed() {
        assert!(serde_json::from_str::<PermissionSet>(r#"{"allow_all":true}"#).is_err());
        assert!(
            serde_json::from_str::<PermissionSet>(r#"{"network":{"origins":[],"bypass":true}}"#)
                .is_err()
        );
    }
    #[test]
    fn documented_limits_receive_bounded_optional_defaults_but_explicit_zero_denies() {
        let limits: ResourceLimits =
            serde_json::from_str(r#"{"fuel_per_call":100,"memory_mib":128,"wall_ms":3000}"#)
                .unwrap();
        assert_eq!(limits.table_elements, 16384);
        assert_eq!(limits.output_bytes, 4 * 1024 * 1024);
        let zero:ResourceLimits=serde_json::from_str(r#"{"fuel_per_call":100,"memory_mib":128,"wall_ms":3000,"table_elements":0,"output_bytes":0}"#).unwrap();
        assert_eq!(zero.table_elements, 0);
        assert_eq!(zero.output_bytes, 0);
        let absent: PermissionSet = serde_json::from_str("{}").unwrap();
        assert_eq!(absent.wasm, ResourceLimits::default());
    }
}
