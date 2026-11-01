//! Authenticated, immutable code dependencies. No Store or host import is shared.
use anyhow::{Context, Result, bail};
use std::collections::{BTreeMap, BTreeSet};
use tobkiri_canon::Digest;
use tobkiri_manifest::{RuntimeSpec, VerifiedPack, source::StagedArtifact};
use wasmtime::{Engine, Module};

const PACK_SOURCE_LIMIT: u64 = 32 * 1024 * 1024;
const CATALOG_SOURCE_LIMIT: u64 = 64 * 1024 * 1024;
type CodeKey = (Digest, u64);

pub(crate) fn declares(pack: &VerifiedPack, name: &str) -> bool {
    matches!(&pack.manifest.runtime, RuntimeSpec::Wasm { core_modules, .. }
        if core_modules.contains_key(name))
}

/// Charge the host's original catalog before constructing any dependency index.
/// Covers bounded names, map nodes, module handles and both temporary sets. JIT
/// code remains subject to the original compilation-input bounds below.
pub(crate) fn metadata_bytes(packs: &[VerifiedPack]) -> Result<u64> {
    let mut count = 0u64;
    for pack in packs {
        if let RuntimeSpec::Wasm { core_modules, .. } = &pack.manifest.runtime {
            if core_modules.len() > tobkiri_manifest::MAX_CORE_MODULE_IMPORTS {
                bail!("bounded core module import count exceeded")
            }
            count = count
                .checked_add(core_modules.len() as u64)
                .context("core module import count overflow")?;
        }
    }
    count
        .checked_mul(4096)
        .context("core module metadata overflow")
}

/// Roots count per logical Pack, just as before. Only explicitly declared,
/// immutable dependency code can share its source allowance by captured digest
/// and length. Each Pack's complete distinct closure still fits 32 MiB.
pub(crate) fn validate_source_budget(packs: &[VerifiedPack]) -> Result<()> {
    let mut budget = SourceBudget::default();
    for pack in packs {
        let RuntimeSpec::Wasm {
            component,
            core_modules,
            ..
        } = &pack.manifest.runtime
        else {
            continue;
        };
        let root = pack
            .files
            .get(component)
            .context("verified component bytes missing")?;
        let modules = core_modules.values().map(|path| {
            pack.files
                .get(path)
                .context("verified core module bytes missing")
        });
        budget.add_pack(root, modules)?;
    }
    Ok(())
}

#[derive(Default)]
struct SourceBudget {
    bytes: u64,
    modules: BTreeSet<CodeKey>,
}
impl SourceBudget {
    fn add_pack<'a>(
        &mut self,
        root: &StagedArtifact,
        modules: impl Iterator<Item = Result<&'a StagedArtifact>>,
    ) -> Result<()> {
        let mut closure = root.len();
        self.bytes = self
            .bytes
            .checked_add(root.len())
            .context("component source overflow")?;
        let mut distinct = BTreeSet::new();
        for source in modules {
            let source = source?;
            let key = (source.digest().clone(), source.len());
            if distinct.insert(key.clone()) {
                closure = closure
                    .checked_add(source.len())
                    .context("Pack source overflow")?;
            }
            if self.modules.insert(key) {
                self.bytes = self
                    .bytes
                    .checked_add(source.len())
                    .context("catalog source overflow")?;
            }
        }
        if closure > PACK_SOURCE_LIMIT {
            bail!("component dependency closure exceeds bounded compilation input")
        }
        if self.bytes > CATALOG_SOURCE_LIMIT {
            bail!("component catalog compilation input exceeded")
        }
        Ok(())
    }
}

/// Temporary cache for one Engine/catalog load. Pack instances retain clones of
/// the compiled modules; mutable instances, memories and tables are never here.
#[derive(Default)]
pub(crate) struct CodeCache(BTreeMap<CodeKey, Module>);
impl CodeCache {
    pub(crate) fn load(&mut self, engine: &Engine, source: &StagedArtifact) -> Result<Module> {
        let key = (source.digest().clone(), source.len());
        if let Some(code) = self.0.get(&key) {
            if !Engine::same(engine, code.engine()) {
                bail!("core module cache belongs to a different Engine")
            }
            // Verify this Pack's actual private bytes even on a cache hit. A
            // matching manifest label alone never supplies executable code.
            source.inspect(|_| Ok(()))?;
            return Ok(code.clone());
        }
        let bytes = source.read()?;
        let code = Module::from_binary(engine, bytes.as_ref())?;
        drop(bytes);
        self.0.insert(key, code.clone());
        Ok(code)
    }

    pub(crate) fn for_pack(
        &mut self,
        engine: &Engine,
        pack: &VerifiedPack,
    ) -> Result<BTreeMap<String, Module>> {
        let mut modules = BTreeMap::new();
        if let RuntimeSpec::Wasm { core_modules, .. } = &pack.manifest.runtime {
            for (name, path) in core_modules {
                let source = pack
                    .files
                    .get(path)
                    .context("verified core module bytes missing")?;
                modules.insert(name.clone(), self.load(engine, source)?);
            }
        }
        Ok(modules)
    }
}

#[cfg(test)]
mod tests;
