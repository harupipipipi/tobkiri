//! Public typed codecs require an actual payload owner. Dynamic UI/Flow values
//! stay opaque; there is no budgetless CanonValue/String/Vec guest conversion.

pub use crate::owned_values::{core_to_val, val_to_core};
