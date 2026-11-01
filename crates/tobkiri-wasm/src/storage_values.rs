//! Exact storage@1.1 typed SQL wire codec. JSON cells keep their mandatory
//! type/value envelopes; old storage@1.0 methods are not reinterpreted here.
use crate::{
    effect_values::{EffectValues, list, text, uint},
    owned_values::ValueOwners,
};
use anyhow::{Context, Result, bail};
use std::fmt::Write;
use tobkiri_canon::{AllocationError, CoreRef, CoreValue};
use tobkiri_store::{OwnedSqlResult, OwnedSqlStatement, SqlResultPolicy};
use wasmtime::component::{BudgetedVecBuilder, Val};

pub(crate) fn statement(params: &[Val], owners: &ValueOwners) -> Result<OwnedSqlStatement> {
    if params.len() < 4 {
        bail!("typed SQL argument arity mismatch")
    }
    let codec = EffectValues(owners);
    let cells = list(&params[2])?;
    let cells = CoreValue::array_from(
        cells
            .iter()
            .map(|cell| codec.parse(cell).map_err(|_| AllocationError::Invalid)),
        owners.graph(),
    )?;
    let value = CoreValue::object_shared(
        [
            Ok((
                "statement",
                CoreValue::string(text(&params[1])?, owners.graph())?,
            )),
            Ok(("params", cells)),
            Ok((
                "max-rows",
                CoreValue::int(i64::from(u32::try_from(uint(&params[3])?)?), owners.graph())?,
            )),
        ]
        .into_iter(),
        owners.graph(),
    )?;
    Ok(OwnedSqlStatement::from_value(value)?)
}

pub(crate) fn policy(value: &Val) -> Result<SqlResultPolicy> {
    let Val::Enum(value) = value else {
        bail!("typed SQL result policy is not its nominal enum")
    };
    match value.as_str() {
        "allow-rows" => Ok(SqlResultPolicy::AllowRows),
        "forbid-rows" => Ok(SqlResultPolicy::ForbidRows),
        _ => bail!("unknown typed SQL result policy"),
    }
}

/// Legacy methods accept JSON scalars rather than typed envelopes. Preserve
/// that boundary while using the same gated native prepare/classify path.
pub(crate) fn legacy_statement(
    params: &[Val],
    query: bool,
    owners: &ValueOwners,
) -> Result<OwnedSqlStatement> {
    let codec = EffectValues(owners);
    let values = list(&params[2])?;
    let cells = CoreValue::array_from(
        values.iter().map(|value| {
            let value = codec.parse(value).map_err(|_| AllocationError::Invalid)?;
            legacy_cell(&value, owners).map_err(|_| AllocationError::Invalid)
        }),
        owners.graph(),
    )?;
    let limit = if query {
        u32::try_from(uint(&params[3])?)?
    } else {
        0
    };
    Ok(OwnedSqlStatement::from_value(CoreValue::object_shared(
        [
            Ok((
                "statement",
                CoreValue::string(text(&params[1])?, owners.graph())?,
            )),
            Ok(("params", cells)),
            Ok((
                "max-rows",
                CoreValue::int(i64::from(limit), owners.graph())?,
            )),
        ]
        .into_iter(),
        owners.graph(),
    )?)?)
}

fn legacy_cell(value: &CoreValue, owners: &ValueOwners) -> Result<CoreValue> {
    let budget = owners.graph();
    let mut number = NumberText {
        bytes: [0; 80],
        length: 0,
    };
    let (kind, value) = match value.inspect() {
        CoreRef::Null => ("null", CoreValue::null(budget)?),
        CoreRef::Bool(value) => (
            "integer",
            CoreValue::string(if value { "1" } else { "0" }, budget)?,
        ),
        CoreRef::Int(value) => {
            write!(&mut number, "{value}")?;
            ("integer", CoreValue::string(number.as_str(), budget)?)
        }
        CoreRef::Str(value) => ("text", CoreValue::string(value, budget)?),
        _ => bail!("legacy SQL parameters require a finite JSON scalar"),
    };
    Ok(CoreValue::object_shared(
        [
            Ok(("type", CoreValue::string(kind, budget)?)),
            Ok(("value", value)),
        ]
        .into_iter(),
        budget,
    )?)
}

pub(crate) fn legacy_result(
    value: &OwnedSqlResult,
    query: bool,
    owners: &ValueOwners,
) -> Result<Vec<Val>> {
    let codec = EffectValues(owners);
    if !query {
        let changed = value
            .value()
            .get_key("affected-rows")
            .context("SQL change count missing")?;
        let changed = changed
            .as_str()
            .context("SQL change count is not text")?
            .parse::<u64>()?;
        return codec.ok(Some(Val::U64(changed)));
    }
    let row_value = value.value().get_key("rows").context("SQL rows missing")?;
    let rows = row_value.array().context("SQL rows are not a list")?;
    let mut output = BudgetedVecBuilder::new(rows.len(), owners.component(), 0)?;
    for row in rows.iter() {
        let row = row.array().context("SQL row is not a list")?;
        let mut cells = BudgetedVecBuilder::new(row.len(), owners.component(), 0)?;
        for cell in row.iter() {
            let kind = cell.get_key("type").context("SQL cell tag missing")?;
            let data = cell.get_key("value").context("SQL cell value missing")?;
            let value = match kind.as_str() {
                Some("null") => codec.string("null")?,
                Some("integer") => {
                    let value = data.as_str().context("SQL integer is not text")?;
                    let parsed = value.parse::<i64>()?;
                    if parsed.unsigned_abs() > tobkiri_canon::MAX_SAFE_INTEGER {
                        bail!(
                            "legacy SQL integer cannot be represented by StrictV1 JSON; use typed SQL"
                        )
                    }
                    codec.string(value)?
                }
                Some("real") => {
                    bail!("legacy SQL real cannot be represented by StrictV1 JSON; use typed SQL")
                }
                Some("text") => codec.json(&data)?,
                Some("blob") => {
                    return Err(anyhow::anyhow!(
                        "legacy SQL blob representation is not declared; use typed SQL"
                    ));
                }
                _ => bail!("unknown SQL result cell"),
            };
            cells.push(value)?;
        }
        output.push(Val::List(cells.finish()?))?;
    }
    codec.ok(Some(Val::List(output.finish()?)))
}

struct NumberText {
    bytes: [u8; 80],
    length: usize,
}
impl NumberText {
    fn as_str(&self) -> &str {
        std::str::from_utf8(&self.bytes[..self.length]).expect("numeric formatter produces UTF-8")
    }
}
impl Write for NumberText {
    fn write_str(&mut self, value: &str) -> std::fmt::Result {
        let end = self
            .length
            .checked_add(value.len())
            .filter(|n| *n <= self.bytes.len())
            .ok_or(std::fmt::Error)?;
        self.bytes[self.length..end].copy_from_slice(value.as_bytes());
        self.length = end;
        Ok(())
    }
}

pub(crate) fn result(value: &OwnedSqlResult, owners: &ValueOwners) -> Result<Vec<Val>> {
    let codec = EffectValues(owners);
    let value = value.value();
    let column_value = value.get_key("columns").context("SQL columns missing")?;
    let columns = column_value.array().context("SQL columns are not a list")?;
    let mut names = BudgetedVecBuilder::new(columns.len(), owners.component(), 0)?;
    for name in columns.iter() {
        names.push(codec.string(name.as_str().context("SQL column is not text")?)?)?;
    }
    let row_value = value.get_key("rows").context("SQL rows missing")?;
    let rows = row_value.array().context("SQL rows are not a list")?;
    let mut output = BudgetedVecBuilder::new(rows.len(), owners.component(), 0)?;
    for row in rows.iter() {
        let row = row.array().context("SQL row is not a list")?;
        let mut cells = BudgetedVecBuilder::new(row.len(), owners.component(), 0)?;
        for cell in row.iter() {
            cells.push(codec.json(&cell)?)?;
        }
        output.push(Val::List(cells.finish()?))?;
    }
    let optional = |name| -> Result<Val> {
        let value = value.get_key(name).context("SQL result metadata missing")?;
        Ok(match value.inspect() {
            CoreRef::Null => Val::Option(None),
            CoreRef::Str(text) => codec.some(codec.string(text)?)?,
            _ => bail!("SQL result metadata is not optional text"),
        })
    };
    codec.ok(Some(
        codec.record(
            [
                ("columns", Val::List(names.finish()?)),
                ("rows", Val::List(output.finish()?)),
                ("affected-rows", optional("affected-rows")?),
                ("last-insert-rowid", optional("last-insert-rowid")?),
            ]
            .into_iter(),
        )?,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    };
    use tobkiri_canon::{AllocationBudget, AllocationRequest};
    use wasmtime::component::{HostAllocationBudget, HostAllocationError, HostAllocationRequest};
    struct Meter(AtomicU64);
    impl AllocationBudget for Meter {
        fn reserve(&self, req: AllocationRequest) -> Result<(), AllocationError> {
            self.0
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |n| {
                    n.checked_add(req.bytes).filter(|n| *n <= 8 * 1024 * 1024)
                })
                .map(|_| ())
                .map_err(|_| AllocationError::Limit)
        }
        fn release(&self, bytes: u64) {
            self.0
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |n| {
                    n.checked_sub(bytes)
                })
                .expect("physical payload must be charged before release");
        }
    }
    impl HostAllocationBudget for Meter {
        fn reserve(&self, req: HostAllocationRequest) -> Result<(), HostAllocationError> {
            AllocationBudget::reserve(
                self,
                AllocationRequest {
                    kind: tobkiri_canon::AllocationKind::Temporary,
                    bytes: req.bytes,
                    elements: req.elements,
                    depth: req.depth,
                    work: req.work,
                },
            )
            .map_err(|_| HostAllocationError::Limit)
        }
        fn release(&self, bytes: u64) {
            AllocationBudget::release(self, bytes)
        }
    }
    fn owners() -> (Arc<Meter>, ValueOwners) {
        let meter = Arc::new(Meter(AtomicU64::new(0)));
        (
            meter.clone(),
            ValueOwners::from_budgets(meter.clone(), meter),
        )
    }

    #[test]
    fn typed_sql_codec_preserves_duplicate_columns_and_lossless_cell_envelopes() {
        let (meter, owners) = owners();
        let value=CoreValue::parse_guest(br#"{"columns":["same","same"],"rows":[[{"type":"integer","value":"9223372036854775807"},{"type":"real","value":"-0e0"}]],"affected-rows":null,"last-insert-rowid":"7"}"#,owners.graph()).unwrap();
        let input = OwnedSqlResult::from_value(value).unwrap();
        let values = result(&input, &owners).unwrap();
        let [Val::Result(Ok(Some(record)))] = values.as_slice() else {
            panic!("typed SQL success result")
        };
        let columns = list(crate::effect_values::field(record, "columns").unwrap()).unwrap();
        assert_eq!(text(&columns[0]).unwrap(), "same");
        assert_eq!(text(&columns[1]).unwrap(), "same");
        let rows = list(crate::effect_values::field(record, "rows").unwrap()).unwrap();
        let cells = list(&rows[0]).unwrap();
        assert_eq!(
            text(&cells[0]).unwrap(),
            r#"{"type":"integer","value":"9223372036854775807"}"#
        );
        assert_eq!(
            text(&cells[1]).unwrap(),
            r#"{"type":"real","value":"-0e0"}"#
        );
        drop(values);
        drop(input);
        drop(owners);
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }

    #[test]
    fn legacy_sql_does_not_reinterpret_arrays_or_lossy_result_cells() {
        let (meter, owners) = owners();
        let array = CoreValue::parse_guest(b"[1]", owners.graph()).unwrap();
        assert!(legacy_cell(&array, &owners).is_err());
        let value=CoreValue::parse_guest(br#"{"columns":["n"],"rows":[[{"type":"integer","value":"9223372036854775807"}]],"affected-rows":null,"last-insert-rowid":null}"#,owners.graph()).unwrap();
        let result = OwnedSqlResult::from_value(value).unwrap();
        assert!(legacy_result(&result, true, &owners).is_err());
        drop(result);
        drop(array);
        drop(owners);
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }
}
