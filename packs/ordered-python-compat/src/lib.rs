//! Pack-owned ordered-finite-json-v2 and the scalar operations used by the
//! original CPython usage-cost and stream-normalizer providers.

mod codec;
mod coercion;
mod unicode;

pub use codec::{
    CodecError, MAX_BYTES, MAX_INTEGER_DIGITS, MAX_NODES, PythonJson, dumps, dumps_python_json,
    loads, loads_python_json,
};
pub use coercion::{CoercionError, pyfloat, pyint, pyrepr, pystr, pystrip, pytruthy};

pub const PROTOCOL: &str = "ordered-finite-json-v2";

/// An ordered native JSON value. Integers use canonical decimal digits, without
/// leading zeroes or a positive sign; integer negative zero is represented as 0.
/// Float values retain their binary64 bits, including negative zero.
#[derive(Debug, PartialEq)]
pub enum PyValue {
    Null,
    Bool(bool),
    Int(String),
    Float(f64),
    String(String),
    Array(Vec<Self>),
    Object(Vec<(String, Self)>),
}

impl PyValue {
    /// Construct a Python dict: repeated keys replace the value in the key's
    /// original position. Wire decoding separately rejects duplicate keys.
    pub fn object(entries: impl IntoIterator<Item = (String, Self)>) -> Self {
        let mut output: Vec<(String, Self)> = Vec::new();
        for (key, value) in entries {
            if let Some((_, current)) = output.iter_mut().find(|(name, _)| name == &key) {
                *current = value;
            } else {
                output.push((key, value));
            }
        }
        Self::Object(output)
    }

    pub fn get(&self, key: &str) -> Option<&Self> {
        self.as_object()?
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value)
    }

    pub fn as_object(&self) -> Option<&[(String, Self)]> {
        if let Self::Object(value) = self {
            Some(value)
        } else {
            None
        }
    }

    pub fn as_array(&self) -> Option<&[Self]> {
        if let Self::Array(value) = self {
            Some(value)
        } else {
            None
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        if let Self::String(value) = self {
            Some(value)
        } else {
            None
        }
    }

    /// Move an object's fields without cloning its descendant tree.
    pub fn into_object(mut self) -> Option<Vec<(String, Self)>> {
        if let Self::Object(value) = &mut self {
            Some(std::mem::take(value))
        } else {
            None
        }
    }

    /// Move an array's elements without cloning its descendant tree.
    pub fn into_array(mut self) -> Option<Vec<Self>> {
        if let Self::Array(value) = &mut self {
            Some(std::mem::take(value))
        } else {
            None
        }
    }

    pub fn into_string(mut self) -> Option<String> {
        if let Self::String(value) = &mut self {
            Some(std::mem::take(value))
        } else {
            None
        }
    }

    pub fn take(&mut self) -> Self {
        std::mem::replace(self, Self::Null)
    }

    fn append_children(&mut self, pending: &mut Vec<Self>) {
        match self {
            Self::Array(values) => {
                if pending.is_empty() {
                    std::mem::swap(pending, values);
                } else {
                    pending.append(values);
                }
            }
            Self::Object(entries) => pending.extend(entries.drain(..).map(|(_, value)| value)),
            _ => {}
        }
    }
}

impl Drop for PyValue {
    fn drop(&mut self) {
        // Empty each owned container before its automatic destructor runs.
        // This also protects error cleanup of a deep, otherwise valid prefix.
        let mut pending = Vec::new();
        self.append_children(&mut pending);
        while let Some(mut value) = pending.pop() {
            value.append_children(&mut pending);
        }
    }
}

impl Clone for PyValue {
    fn clone(&self) -> Self {
        enum Task<'a> {
            Value(&'a PyValue),
            Array(usize),
            Object(&'a [(String, PyValue)]),
        }
        let mut tasks = vec![Task::Value(self)];
        let mut values = Vec::new();
        while let Some(task) = tasks.pop() {
            match task {
                Task::Value(value) => match value {
                    Self::Null => values.push(Self::Null),
                    Self::Bool(value) => values.push(Self::Bool(*value)),
                    Self::Int(value) => values.push(Self::Int(value.clone())),
                    Self::Float(value) => values.push(Self::Float(*value)),
                    Self::String(value) => values.push(Self::String(value.clone())),
                    Self::Array(children) => {
                        tasks.push(Task::Array(children.len()));
                        tasks.extend(children.iter().rev().map(Task::Value));
                    }
                    Self::Object(entries) => {
                        tasks.push(Task::Object(entries));
                        tasks.extend(entries.iter().rev().map(|(_, value)| Task::Value(value)));
                    }
                },
                Task::Array(length) => {
                    let children = values.split_off(values.len() - length);
                    values.push(Self::Array(children));
                }
                Task::Object(entries) => {
                    let children = values.split_off(values.len() - entries.len());
                    values.push(Self::Object(
                        entries
                            .iter()
                            .map(|(key, _)| key.clone())
                            .zip(children)
                            .collect(),
                    ));
                }
            }
        }
        values.pop().expect("one cloned root")
    }
}
