//! Structural ABI descriptors derived from the authoritative core WIT.
use anyhow::{Result, bail};
use std::collections::BTreeMap;
use wit_parser::{Handle, Resolve, Type, TypeDefKind};

#[derive(Clone, Debug)]
pub enum AbiType {
    Primitive(&'static str),
    List(Box<Self>),
    Tuple(Vec<Self>),
    Record(Vec<(String, Self)>),
    Variant(Vec<(String, Option<Self>)>),
    Enum(Vec<String>),
    Flags(Vec<String>),
    Option(Box<Self>),
    Result(Option<Box<Self>>, Option<Box<Self>>),
    Own(usize),
    Borrow(usize),
}
#[derive(Clone, Debug)]
pub struct AbiFunction {
    pub params: Vec<(String, AbiType)>,
    pub result: Option<AbiType>,
}
#[derive(Clone, Debug)]
pub struct AbiInterface {
    pub functions: BTreeMap<String, AbiFunction>,
    pub types: BTreeMap<String, AbiType>,
    pub resources: BTreeMap<String, usize>,
}

pub fn core_abi() -> Result<BTreeMap<String, AbiInterface>> {
    core_abi_version("1.0.0")
}

/// Describe one exact generic ABI without coalescing versioned resources.
pub fn core_abi_version(version: &str) -> Result<BTreeMap<String, AbiInterface>> {
    let (resolve, package) = crate::resolve_core_version(version)?;
    resolve.packages[package]
        .interfaces
        .iter()
        .map(|(name, id)| {
            let interface = &resolve.interfaces[*id];
            let functions = interface
                .functions
                .iter()
                .map(|(name, function)| {
                    Ok((
                        name.clone(),
                        AbiFunction {
                            params: function
                                .params
                                .iter()
                                .map(|p| Ok((p.name.clone(), describe(&resolve, p.ty)?)))
                                .collect::<Result<_>>()?,
                            result: function
                                .result
                                .map(|ty| describe(&resolve, ty))
                                .transpose()?,
                        },
                    ))
                })
                .collect::<Result<_>>()?;
            let mut types = BTreeMap::new();
            let mut resources = BTreeMap::new();
            for (name, id) in &interface.types {
                let mut original = *id;
                while let TypeDefKind::Type(Type::Id(next)) = resolve.types[original].kind {
                    original = next;
                }
                if matches!(resolve.types[original].kind, TypeDefKind::Resource) {
                    resources.insert(name.clone(), original.index());
                } else {
                    types.insert(name.clone(), describe(&resolve, Type::Id(*id))?);
                }
            }
            Ok((
                name.clone(),
                AbiInterface {
                    functions,
                    types,
                    resources,
                },
            ))
        })
        .collect()
}
fn describe(resolve: &Resolve, ty: Type) -> Result<AbiType> {
    use AbiType as A;
    Ok(match ty {
        Type::Bool => A::Primitive("bool"),
        Type::U8 => A::Primitive("u8"),
        Type::U16 => A::Primitive("u16"),
        Type::U32 => A::Primitive("u32"),
        Type::U64 => A::Primitive("u64"),
        Type::S8 => A::Primitive("s8"),
        Type::S16 => A::Primitive("s16"),
        Type::S32 => A::Primitive("s32"),
        Type::S64 => A::Primitive("s64"),
        Type::F32 => A::Primitive("f32"),
        Type::F64 => A::Primitive("f64"),
        Type::Char => A::Primitive("char"),
        Type::String => A::Primitive("string"),
        Type::Id(id) => match &resolve.types[id].kind {
            TypeDefKind::Type(ty) => describe(resolve, *ty)?,
            TypeDefKind::List(ty) => A::List(Box::new(describe(resolve, *ty)?)),
            TypeDefKind::Tuple(t) => A::Tuple(
                t.types
                    .iter()
                    .map(|ty| describe(resolve, *ty))
                    .collect::<Result<_>>()?,
            ),
            TypeDefKind::Record(r) => A::Record(
                r.fields
                    .iter()
                    .map(|f| Ok((f.name.clone(), describe(resolve, f.ty)?)))
                    .collect::<Result<_>>()?,
            ),
            TypeDefKind::Variant(v) => A::Variant(
                v.cases
                    .iter()
                    .map(|c| {
                        Ok((
                            c.name.clone(),
                            c.ty.map(|ty| describe(resolve, ty)).transpose()?,
                        ))
                    })
                    .collect::<Result<_>>()?,
            ),
            TypeDefKind::Enum(e) => A::Enum(e.cases.iter().map(|c| c.name.clone()).collect()),
            TypeDefKind::Flags(f) => A::Flags(f.flags.iter().map(|f| f.name.clone()).collect()),
            TypeDefKind::Option(ty) => A::Option(Box::new(describe(resolve, *ty)?)),
            TypeDefKind::Result(r) => A::Result(
                r.ok.map(|t| describe(resolve, t).map(Box::new))
                    .transpose()?,
                r.err
                    .map(|t| describe(resolve, t).map(Box::new))
                    .transpose()?,
            ),
            TypeDefKind::Handle(Handle::Own(id)) => A::Own(resource_index(resolve, *id)),
            TypeDefKind::Handle(Handle::Borrow(id)) => A::Borrow(resource_index(resolve, *id)),
            TypeDefKind::Resource => A::Own(id.index()),
            _ => bail!("unsupported generic ABI type"),
        },
        _ => bail!("unsupported generic ABI primitive"),
    })
}
fn resource_index(resolve: &Resolve, mut id: wit_parser::TypeId) -> usize {
    while let TypeDefKind::Type(Type::Id(next)) = resolve.types[id].kind {
        id = next;
    }
    id.index()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn secret_handle_aliases_retain_the_same_resource_identity() {
        let abi = core_abi().unwrap();
        let id = abi["secrets"].resources["secret"];
        assert_eq!(abi["network"].resources["secret"], id);
        let AbiType::Result(Some(ok), _) = abi["secrets"].functions["get"].result.as_ref().unwrap()
        else {
            panic!("secret result missing")
        };
        assert!(matches!(ok.as_ref(),AbiType::Own(actual)if *actual==id));
        let AbiType::List(element) = &abi["network"].functions["fetch"].params[1].1 else {
            panic!("opaque header secret list missing")
        };
        assert!(matches!(element.as_ref(),AbiType::Borrow(actual)if *actual==id));
    }
}
