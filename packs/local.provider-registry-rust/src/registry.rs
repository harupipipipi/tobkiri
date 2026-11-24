//! Sequential pure translation of ProviderRegistry's six connection methods.
//!
//! Pack record/policy rules stay Pack-owned. Store and clock are explicit
//! dependencies; this module does not implement SQL, filesystem locks, Gate
//! authority, credential mutation, migration, or concurrency equivalence.
use crate::{
    failure::{SourceError, SourceResult},
    record,
    state::{
        Clock, DocumentStore, NONE, STORE_VERSION, float, get, increment_record_revision,
        increment_store_revision, integer, object, put, remove, required, text,
    },
};
use ordered_python_compat::{PyValue, pystr, pytruthy};

pub struct Registry<S: DocumentStore, C: Clock> {
    profile_id: String,
    store: S,
    clock: C,
}
impl<S: DocumentStore, C: Clock> Registry<S, C> {
    pub fn new(profile_id: &PyValue, store: S, clock: C) -> SourceResult<Self> {
        // Original construction validates before any store access.
        let profile_id = record::validate_profile_id(profile_id)?;
        Ok(Self {
            profile_id,
            store,
            clock,
        })
    }
    pub fn profile_id(&self) -> &str {
        &self.profile_id
    }
    pub fn store(&self) -> &S {
        &self.store
    }
    pub fn store_mut(&mut self) -> &mut S {
        &mut self.store
    }
    pub fn into_parts(self) -> (S, C) {
        (self.store, self.clock)
    }

    fn empty(&self) -> PyValue {
        object([
            ("version", text(STORE_VERSION)),
            ("profile_id", text(&self.profile_id)),
            ("revision", PyValue::Int("0".into())),
            ("providers", PyValue::Object(Vec::new())),
            ("migration", PyValue::Null),
        ])
    }
    fn read(&mut self) -> SourceResult<PyValue> {
        let Some(state) = self.store.read()? else {
            return Ok(self.empty());
        };
        if state.as_object().is_none() || get(&state, "version").as_str() != Some(STORE_VERSION) {
            return Err(SourceError::value(
                "provider registry store version is invalid",
            ));
        }
        if get(&state, "profile_id").as_str() != Some(self.profile_id.as_str()) {
            return Err(SourceError::value(
                "provider registry profile binding is invalid",
            ));
        }
        let Some(providers) = get(&state, "providers").as_object() else {
            return Err(SourceError::value("provider registry store is invalid"));
        };
        // Validate raw stored records before revision lookup/conversion. Never
        // re-normalize the records (notably their legacy health evidence).
        for (_, stored) in providers {
            if stored.as_object().is_none() {
                return Err(SourceError::value("provider registry record is invalid"));
            }
            record::effective_model_access(stored)?;
            record::provider_endpoint(
                get(stored, "endpoint"),
                get(stored, "credential_handle"),
                get(stored, "adapter_id"),
            )?;
        }
        Ok(state)
    }
    fn assert_revision(state: &PyValue, expected: &PyValue) -> SourceResult<()> {
        let zero = PyValue::Int("0".into());
        let raw = get(state, "revision");
        let actual = integer(if pytruthy(raw) { raw } else { &zero })?;
        let expected = integer(expected)?;
        if actual != expected {
            return Err(SourceError::conflict("provider registry revision is stale"));
        }
        Ok(())
    }
    fn providers(state: &PyValue) -> &PyValue {
        get(state, "providers")
    }
    fn current<'a>(state: &'a PyValue, key: &str) -> &'a PyValue {
        Self::providers(state).get(key).unwrap_or(&NONE)
    }
    fn install(state: &mut PyValue, key: &str, record: PyValue) {
        let PyValue::Object(fields) = state else {
            unreachable!("validated store dict")
        };
        let providers = fields
            .iter_mut()
            .find(|(name, _)| name == "providers")
            .expect("validated providers dict");
        put(&mut providers.1, key, record);
    }
    fn increment_revision(state: &mut PyValue) -> SourceResult<()> {
        let revision = increment_store_revision(required(state, "revision")?)?;
        put(state, "revision", revision);
        Ok(())
    }

    pub fn snapshot(&mut self) -> SourceResult<PyValue> {
        let state = self.read()?;
        let mut providers = Self::providers(&state)
            .as_object()
            .expect("validated providers dict")
            .iter()
            .collect::<Vec<_>>();
        providers.sort_by(|(left, _), (right, _)| left.cmp(right));
        Ok(object([
            ("version", required(&state, "version")?.clone()),
            ("profile_id", text(&self.profile_id)),
            ("revision", required(&state, "revision")?.clone()),
            (
                "providers",
                PyValue::Array(
                    providers
                        .into_iter()
                        .map(|(_, value)| value.clone())
                        .collect(),
                ),
            ),
        ]))
    }

    pub fn health(&mut self) -> SourceResult<PyValue> {
        let snapshot = self.snapshot()?;
        let mut providers = Vec::new();
        for item in get(&snapshot, "providers")
            .as_array()
            .expect("snapshot providers list")
        {
            let evidence = get(item, "health_evidence");
            let evidence = if evidence.as_object().is_some() {
                evidence
            } else {
                &NONE
            };
            let verified = matches!(get(evidence, "verified"), PyValue::Bool(true));
            let raw_status = get(evidence, "status");
            let mut status = if pytruthy(raw_status) {
                pystr(raw_status)
            } else {
                "unknown".into()
            };
            if !verified || !matches!(status.as_str(), "available" | "unavailable") {
                status = "unknown".into();
            }
            // Dict-literal evaluation reads the ID before converting time.
            let provider_instance_id = required(item, "provider_instance_id")?.clone();
            let observed = get(evidence, "observed_at");
            // Status may be unknown while verified timestamp conversion still
            // executes. `is True` is deliberately distinct from truthiness.
            let observed = if verified && !matches!(observed, PyValue::Null) {
                PyValue::Float(float(observed)?)
            } else {
                PyValue::Null
            };
            providers.push(object([
                ("provider_instance_id", provider_instance_id),
                ("status", text(status)),
                ("observed_at", observed),
                ("verified", PyValue::Bool(verified)),
            ]));
        }
        Ok(object([("providers", PyValue::Array(providers))]))
    }

    pub fn prepare_save(
        &mut self,
        record: &PyValue,
        expected_revision: &PyValue,
    ) -> SourceResult<PyValue> {
        let mut normalized = record::normalize_record(record)?;
        let state = self.read()?;
        Self::assert_revision(&state, expected_revision)?;
        let key = required(&normalized, "provider_instance_id")?
            .as_str()
            .expect("normalized provider id");
        let current = Self::current(&state, key);
        let empty = PyValue::Object(Vec::new());
        let current = if matches!(current, PyValue::Null) {
            &empty
        } else {
            current
        };
        record::retain_model_access(&mut normalized, current)?;
        let revision = get(current, "record_revision");
        let zero = PyValue::Int("0".into());
        let revision =
            increment_record_revision(if pytruthy(revision) { revision } else { &zero })?;
        put(&mut normalized, "record_revision", revision);
        Ok(normalized)
    }

    pub fn save(&mut self, record: &PyValue, expected_revision: &PyValue) -> SourceResult<PyValue> {
        let mut normalized = record::normalize_record(record)?;
        let mut state = self.read()?;
        Self::assert_revision(&state, expected_revision)?;
        let key = required(&normalized, "provider_instance_id")?
            .as_str()
            .expect("normalized provider id")
            .to_owned();
        let current = Self::current(&state, &key);
        let empty = PyValue::Object(Vec::new());
        let current = if matches!(current, PyValue::Null) {
            &empty
        } else {
            current
        };
        record::retain_model_access(&mut normalized, current)?;
        let now = self.clock.now()?;
        let created = get(current, "created_at");
        let created = if pytruthy(created) {
            pystr(created)
        } else {
            now.clone()
        };
        put(&mut normalized, "created_at", text(created));
        put(&mut normalized, "updated_at", text(now));
        let revision = get(current, "record_revision");
        let zero = PyValue::Int("0".into());
        put(
            &mut normalized,
            "record_revision",
            increment_record_revision(if pytruthy(revision) { revision } else { &zero })?,
        );
        Self::install(&mut state, &key, normalized.clone());
        Self::increment_revision(&mut state)?;
        self.store.write(&state)?;
        Ok(object([
            ("action", text("saved")),
            ("provider", normalized),
            ("store_revision", required(&state, "revision")?.clone()),
        ]))
    }

    pub fn set_model_access(
        &mut self,
        provider_instance_id: &PyValue,
        policy: &PyValue,
        expected_revision: &PyValue,
    ) -> SourceResult<PyValue> {
        let key = record::identifier(provider_instance_id)?;
        let mut state = self.read()?;
        Self::assert_revision(&state, expected_revision)?;
        let current = Self::current(&state, &key);
        if current.as_object().is_none() {
            return Err(SourceError::key_error("provider instance is unknown"));
        }
        let normalized = record::normalize_model_access(policy, current)?;
        let mut updated = current.clone();
        put(&mut updated, "model_access", normalized.clone());
        put(&mut updated, "updated_at", text(self.clock.now()?));
        let revision = get(current, "record_revision");
        let zero = PyValue::Int("0".into());
        put(
            &mut updated,
            "record_revision",
            increment_record_revision(if pytruthy(revision) { revision } else { &zero })?,
        );
        Self::install(&mut state, &key, updated);
        Self::increment_revision(&mut state)?;
        self.store.write(&state)?;
        Ok(object([
            ("provider_instance_id", text(key)),
            ("profile_id", text(&self.profile_id)),
            ("model_access", normalized),
            ("store_revision", required(&state, "revision")?.clone()),
        ]))
    }

    pub fn delete(
        &mut self,
        provider_instance_id: &PyValue,
        expected_revision: &PyValue,
    ) -> SourceResult<PyValue> {
        let key = record::identifier(provider_instance_id)?;
        let mut state = self.read()?;
        Self::assert_revision(&state, expected_revision)?;
        if Self::providers(&state).get(&key).is_none() {
            return Err(SourceError::key_error("provider instance is unknown"));
        }
        let PyValue::Object(fields) = &mut state else {
            unreachable!("validated store dict")
        };
        let providers = fields
            .iter_mut()
            .find(|(name, _)| name == "providers")
            .expect("validated providers dict");
        remove(&mut providers.1, &key);
        Self::increment_revision(&mut state)?;
        self.store.write(&state)?;
        Ok(object([
            ("action", text("deleted")),
            ("provider_instance_id", text(key)),
            ("store_revision", required(&state, "revision")?.clone()),
        ]))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::MemoryStore;

    #[derive(Default)]
    struct TestClock {
        calls: usize,
    }
    impl Clock for TestClock {
        fn now(&mut self) -> SourceResult<String> {
            self.calls += 1;
            Ok(format!("2026-01-02T03:04:{:02}Z", self.calls))
        }
    }
    fn revision(value: &str) -> PyValue {
        PyValue::Int(value.into())
    }
    fn connection() -> PyValue {
        object([
            ("provider_instance_id", text("connection/work")),
            ("adapter_id", text("openai-compatible")),
            ("credential_handle", text("credential:synthetic-test")),
            ("endpoint", text("https://provider.example/v1")),
            (
                "metadata",
                object([("catalog_provider_id", text("vendor"))]),
            ),
        ])
    }
    fn policy() -> PyValue {
        object([
            ("version", text("tobkiri.connection-model-access.v1")),
            ("mode", text("explicit")),
            ("model_ids", PyValue::Array(vec![text("vendor/model-a")])),
        ])
    }
    fn fresh() -> Registry<MemoryStore, TestClock> {
        Registry::new(
            &text("defaults"),
            MemoryStore::absent(),
            TestClock::default(),
        )
        .unwrap()
    }
    fn stored(providers: PyValue, revision: PyValue) -> PyValue {
        object([
            ("version", text(STORE_VERSION)),
            ("profile_id", text("defaults")),
            ("revision", revision),
            ("providers", providers),
            ("migration", PyValue::Null),
        ])
    }

    #[test]
    fn preflight_keeps_absent_store_and_clock_but_repeated_saves_advance_revisions() {
        let mut registry = fresh();
        let input = connection();
        let prepared = registry.prepare_save(&input, &revision("0")).unwrap();
        assert_eq!(get(&prepared, "record_revision"), &revision("1"));
        assert!(registry.store().document().is_none());
        assert_eq!(registry.clock.calls, 0);
        let first = registry.save(&input, &revision("0")).unwrap();
        let second = registry.save(&input, &revision("1")).unwrap();
        assert_eq!(get(&second, "store_revision"), &revision("2"));
        let first = get(&first, "provider");
        let second = get(&second, "provider");
        assert_eq!(get(second, "created_at"), get(first, "created_at"));
        assert_ne!(get(second, "updated_at"), get(first, "updated_at"));
        assert_eq!(get(second, "record_revision"), &revision("2"));
        assert_eq!(registry.clock.calls, 2);
        assert_eq!(registry.store().write_count(), 2);
        // Caller input is never populated with saved timestamps/revisions.
        assert!(input.get("record_revision").is_none());
    }

    #[test]
    fn normalization_and_stored_record_validation_precede_revision_checks() {
        let raw = stored(
            object([(
                "stored",
                object([
                    ("adapter_id", text("openai-compatible")),
                    ("credential_handle", text("credential:opaque")),
                    ("endpoint", text("http://provider.example/v1")),
                ]),
            )]),
            revision("7"),
        );
        let mut registry = Registry::new(
            &text("defaults"),
            MemoryStore::initialized(raw.clone()),
            TestClock::default(),
        )
        .unwrap();
        let error = registry.save(&object([]), &revision("0")).unwrap_err();
        assert_eq!(error.message, "provider registry identifier is invalid");
        let error = registry.save(&connection(), &revision("0")).unwrap_err();
        assert_eq!(
            error.message,
            "credentialed provider endpoint requires HTTPS"
        );
        assert_eq!(registry.store().document(), Some(&raw));
        assert_eq!(registry.store().write_count(), 0);
        assert_eq!(registry.clock.calls, 0);
    }

    #[test]
    fn policy_retention_uses_the_exact_route_and_preflight_reserves_nothing() {
        let mut registry = fresh();
        let mut first = connection();
        put(&mut first, "model_access", policy());
        registry.save(&first, &revision("0")).unwrap();
        let kept = registry
            .prepare_save(&connection(), &revision("1"))
            .unwrap();
        assert!(kept.get("model_access").is_some());
        assert_eq!(registry.clock.calls, 1);
        assert_eq!(registry.store().write_count(), 1);
        let mut changed = connection();
        put(
            &mut changed,
            "endpoint",
            text("https://provider.example/other/v1"),
        );
        let error = registry.save(&changed, &revision("1")).unwrap_err();
        assert_eq!(
            error.message,
            "provider route changed; create a new connection or explicitly replace model access for the new connection"
        );
        assert_eq!(registry.clock.calls, 1);
        put(&mut changed, "model_access", policy());
        registry.save(&changed, &revision("1")).unwrap();
        assert_eq!(registry.clock.calls, 2);
    }

    #[test]
    fn policy_unknown_checks_stale_revision_before_existence_and_policy() {
        let mut registry = fresh();
        let unknown = text("missing");
        let invalid = object([("invalid", PyValue::Bool(true))]);
        let stale = registry
            .set_model_access(&unknown, &invalid, &revision("1"))
            .unwrap_err();
        assert_eq!(stale.exception_class, "ProviderRegistryConflict");
        let missing = registry
            .set_model_access(&unknown, &invalid, &revision("0"))
            .unwrap_err();
        assert_eq!(missing.exception_class, "KeyError");
        assert_eq!(missing.args, vec![text("provider instance is unknown")]);
        assert_eq!(registry.clock.calls, 0);
        assert!(registry.store().document().is_none());
    }

    #[test]
    fn raw_health_is_not_normalized_on_read_and_id_precedes_timestamp_conversion() {
        let mut current = connection();
        put(
            &mut current,
            "health_evidence",
            object([
                ("verified", revision("1")),
                ("status", text("available")),
                ("observed_at", text("bad-time")),
            ]),
        );
        let raw = stored(
            object([("connection/work", current.clone())]),
            revision("1"),
        );
        let mut registry = Registry::new(
            &text("defaults"),
            MemoryStore::initialized(raw.clone()),
            TestClock::default(),
        )
        .unwrap();
        let health = registry.health().unwrap();
        let health = &get(&health, "providers").as_array().unwrap()[0];
        assert_eq!(get(health, "verified"), &PyValue::Bool(false));
        assert_eq!(get(health, "observed_at"), &PyValue::Null);
        assert_eq!(registry.store().document(), Some(&raw));
        remove(&mut current, "provider_instance_id");
        put(
            &mut current,
            "health_evidence",
            object([
                ("verified", PyValue::Bool(true)),
                ("observed_at", text("bad-time")),
            ]),
        );
        registry.store = MemoryStore::initialized(stored(
            object([("connection/work", current)]),
            revision("1"),
        ));
        let error = registry.health().unwrap_err();
        assert_eq!(error.exception_class, "KeyError");
        assert_eq!(error.args, vec![text("provider_instance_id")]);
    }

    #[test]
    fn stored_revision_keeps_original_numeric_type_and_unknown_fields() {
        let mut raw = stored(object([]), PyValue::Float(1.5));
        put(
            &mut raw,
            "unrecognized",
            object([("kept", PyValue::Bool(true))]),
        );
        let mut registry = Registry::new(
            &text("defaults"),
            MemoryStore::initialized(raw),
            TestClock::default(),
        )
        .unwrap();
        let saved = registry.save(&connection(), &revision("1")).unwrap();
        assert_eq!(get(&saved, "store_revision"), &PyValue::Float(2.5));
        assert_eq!(
            registry.store().document().unwrap().get("unrecognized"),
            Some(&object([("kept", PyValue::Bool(true))]))
        );
    }

    #[test]
    fn missing_revision_fails_at_increment_after_clock_without_writing() {
        let mut raw = stored(object([]), revision("0"));
        remove(&mut raw, "revision");
        let mut registry = Registry::new(
            &text("defaults"),
            MemoryStore::initialized(raw.clone()),
            TestClock::default(),
        )
        .unwrap();
        let error = registry.save(&connection(), &revision("0")).unwrap_err();
        assert_eq!(error.exception_class, "KeyError");
        assert_eq!(error.args, vec![text("revision")]);
        assert_eq!(registry.clock.calls, 1);
        assert_eq!(registry.store().write_count(), 0);
        assert_eq!(registry.store().document(), Some(&raw));
    }

    struct FailingWrite {
        raw: Option<PyValue>,
        attempted: usize,
    }
    impl DocumentStore for FailingWrite {
        fn read(&mut self) -> SourceResult<Option<PyValue>> {
            Ok(self.raw.clone())
        }
        fn write(&mut self, _: &PyValue) -> SourceResult<()> {
            self.attempted += 1;
            // An explicit adapter fault, not a fabricated original observation.
            Err(SourceError {
                exception_class: "OSError",
                message: "[Errno 28] No space left on device".into(),
                args: vec![revision("28"), text("No space left on device")],
            })
        }
    }
    #[test]
    fn structured_adapter_write_failure_propagates_without_a_success_projection() {
        let mut registry = Registry::new(
            &text("defaults"),
            FailingWrite {
                raw: None,
                attempted: 0,
            },
            TestClock::default(),
        )
        .unwrap();
        let error = registry.save(&connection(), &revision("0")).unwrap_err();
        assert_eq!(error.exception_class, "OSError");
        assert_eq!(error.message, "[Errno 28] No space left on device");
        assert_eq!(
            error.args,
            vec![revision("28"), text("No space left on device")]
        );
        assert_eq!(registry.store().attempted, 1);
        assert!(registry.store().raw.is_none());
        assert_eq!(registry.clock.calls, 1);
    }

    #[test]
    fn policy_update_and_delete_preserve_handle_fields_and_clock_order() {
        let mut registry = fresh();
        let saved = registry.save(&connection(), &revision("0")).unwrap();
        let saved = get(&saved, "provider");
        registry
            .set_model_access(&text("connection/work"), &policy(), &revision("1"))
            .unwrap();
        let snapshot = registry.snapshot().unwrap();
        let updated = &get(&snapshot, "providers").as_array().unwrap()[0];
        assert_eq!(
            get(updated, "credential_handle"),
            get(saved, "credential_handle")
        );
        assert_eq!(get(updated, "created_at"), get(saved, "created_at"));
        assert_ne!(get(updated, "updated_at"), get(saved, "updated_at"));
        assert_eq!(get(updated, "record_revision"), &revision("2"));
        registry
            .delete(&text("connection/work"), &revision("2"))
            .unwrap();
        assert_eq!(registry.clock.calls, 2);
        assert_eq!(registry.store().write_count(), 3);
        assert!(registry.store().document().is_some());
        let snapshot = registry.snapshot().unwrap();
        assert_eq!(get(&snapshot, "revision"), &revision("3"));
        assert_eq!(get(&snapshot, "providers"), &PyValue::Array(vec![]));
    }

    #[test]
    fn snapshot_sorts_store_keys_without_reordering_or_normalizing_raw_records() {
        let mut a = connection();
        put(&mut a, "provider_instance_id", text("record-a"));
        let mut z = connection();
        put(&mut z, "provider_instance_id", text("record-z"));
        let raw = stored(
            object([("z-key", a.clone()), ("a-key", z.clone())]),
            revision("0"),
        );
        let mut registry = Registry::new(
            &text("defaults"),
            MemoryStore::initialized(raw.clone()),
            TestClock::default(),
        )
        .unwrap();
        let snapshot = registry.snapshot().unwrap();
        assert_eq!(get(&snapshot, "providers"), &PyValue::Array(vec![z, a]));
        assert_eq!(registry.store().document(), Some(&raw));
        assert_eq!(registry.store().write_count(), 0);
        assert_eq!(registry.clock.calls, 0);
    }
}
