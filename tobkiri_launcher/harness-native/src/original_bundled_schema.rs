//! Verification of the original bundled catalog document families.
use super::{external_catalog, json, profile_schema, *};
use tobkiri_canon::AllocationBudget;

/// The original bundle lock's finite document kinds.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum CatalogDocumentKind {
    Pack,
    ExecutableCatalog,
    Base,
    Shell,
    Profile,
}

/// Funded copies of the fixed schema resources for a single CatalogIo invocation.
#[derive(Default)]
pub(super) struct CatalogSchemas {
    documents: [Option<Funded<Document>>; RESOURCES.len()],
}

impl CatalogSchemas {
    #[cfg(test)]
    pub(super) fn validate_document<S: OriginalAuthorityOwner>(
        &mut self,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
        kind: CatalogDocumentKind,
        raw: &[u8],
    ) -> Result<bool> {
        Ok(self.prove_document(op, budget, kind, raw)?.is_some())
    }

    /// Prove the structure and semantics of the exact digest-checked bytes.
    pub(super) fn prove_document<S: OriginalAuthorityOwner>(
        &mut self,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
        kind: CatalogDocumentKind,
        raw: &[u8],
    ) -> Result<Option<Funded<Document>>> {
        op.check(OriginalAuthorityStage::Source)?;
        let document = match json::decode_read_only_strict(raw, op, budget) {
            Ok(document) => document,
            Err(error)
                if matches!(
                    error.class,
                    "tobkiri_protocol.errors.CanonicalizationError" | "builtins.ValueError"
                ) || matches!(error.cause, Some(OriginalAuthorityCause::Unsupported(_))) =>
            {
                return Ok(None);
            }
            Err(error) => return Err(error),
        };
        let Some(schema) = SchemaResource::select(kind, &document) else {
            return Ok(None);
        };
        let uses_projection = matches!(
            schema,
            SchemaResource::ProfileV4 | SchemaResource::ProfileV5
        );
        let common = SchemaResource::Common;
        let projection = SchemaResource::ProfileProjection;
        self.load(schema, op, budget)?;
        self.load(common, op, budget)?;
        if uses_projection {
            self.load(projection, op, budget)?;
        }
        let common = self.loaded(common);
        let closure = [
            self.loaded(schema),
            common,
            if uses_projection {
                self.loaded(projection)
            } else {
                common
            },
        ];
        let result = profile_schema::matches_shape(
            op,
            budget,
            &closure[..2 + usize::from(uses_projection)],
            &[schema.resource().0],
            &document,
            document.root,
        );
        match result {
            Ok(true) => {
                let proven =
                    external_catalog::validate_bundled_semantics(&document, kind, op, budget)?;
                Ok(proven.then_some(document))
            }
            Ok(false) => Ok(None),
            Err(error) if matches!(error.cause, Some(OriginalAuthorityCause::Unsupported(_))) => {
                Ok(None)
            }
            Err(error) => Err(error),
        }
    }

    fn load<S: OriginalAuthorityOwner>(
        &mut self,
        schema: SchemaResource,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<()> {
        let slot = &mut self.documents[schema as usize];
        if slot.is_none() {
            *slot = Some(json::decode_read_only_strict(
                schema.resource().1,
                op,
                budget,
            )?);
        }
        Ok(())
    }

    fn loaded(&self, schema: SchemaResource) -> &Document {
        self.documents[schema as usize]
            .as_deref()
            .expect("catalog schema resource has been loaded")
    }
}

#[derive(Clone, Copy)]
enum SchemaResource {
    Pack,
    ExecutableCatalog,
    Base,
    ShellV4,
    ShellV5,
    ProfileV4,
    ProfileV5,
    Common,
    ProfileProjection,
}

impl SchemaResource {
    fn select(kind: CatalogDocumentKind, document: &Document) -> Option<Self> {
        let version_is = |field, expected| {
            document
                .get(document.root, field)
                .is_some_and(|version| document.string_is(version, expected))
        };
        Some(match kind {
            CatalogDocumentKind::Pack => Self::Pack,
            CatalogDocumentKind::ExecutableCatalog => Self::ExecutableCatalog,
            CatalogDocumentKind::Base => Self::Base,
            CatalogDocumentKind::Shell
                if version_is("shell_api_version", "io.tobkiri.shell.v4") =>
            {
                Self::ShellV4
            }
            CatalogDocumentKind::Shell
                if version_is("shell_api_version", "io.tobkiri.shell.v5") =>
            {
                Self::ShellV5
            }
            CatalogDocumentKind::Profile
                if version_is("profile_api_version", "io.tobkiri.profile.v4") =>
            {
                Self::ProfileV4
            }
            CatalogDocumentKind::Profile
                if version_is("profile_api_version", "io.tobkiri.profile.v5") =>
            {
                Self::ProfileV5
            }
            CatalogDocumentKind::Shell | CatalogDocumentKind::Profile => return None,
        })
    }

    fn resource(self) -> (&'static str, &'static [u8]) {
        RESOURCES[self as usize]
    }
}

macro_rules! schema_bytes {
    ($filename:literal) => {
        include_bytes!(concat!(
            "../../../tobkiri_runtime/tobkiri_protocol/schemas/",
            $filename,
            ".schema.json"
        ))
    };
}

const RESOURCES: [(&str, &[u8]); 9] = [
    (
        "https://schemas.tobkiri.local/io.tobkiri/pack-manifest.v4.schema.json",
        schema_bytes!("pack_manifest_v4"),
    ),
    (
        "https://schemas.tobkiri.local/io.tobkiri/executable-catalog.v4.schema.json",
        schema_bytes!("executable_catalog_v4"),
    ),
    (
        "https://schemas.tobkiri.local/io.tobkiri/base-definition.v4.schema.json",
        schema_bytes!("base_definition_v4"),
    ),
    (
        "https://schemas.tobkiri.local/io.tobkiri/shell-definition.v4.schema.json",
        schema_bytes!("shell_definition_v4"),
    ),
    (
        "https://schemas.tobkiri.local/io.tobkiri/shell-definition.v5.schema.json",
        schema_bytes!("shell_definition_v5"),
    ),
    (profile_schema::PROFILE_V4, schema_bytes!("profile_v4")),
    (profile_schema::PROFILE_V5, schema_bytes!("profile_v5")),
    (profile_schema::COMMON, schema_bytes!("common_v1")),
    (
        profile_schema::PROJECTION,
        schema_bytes!("profile_content_projection_v1"),
    ),
];
