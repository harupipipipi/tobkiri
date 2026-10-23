use crate::PyValue;
use serde_json::value::RawValue;
use std::{
    collections::{BTreeMap, BTreeSet},
    error::Error,
    fmt, io,
};
use tobkiri_canon::{CanonValue, Variant};

pub const MAX_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_NODES: usize = 100_000;
pub const MAX_INTEGER_DIGITS: usize = 4300;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CodecError {
    Invalid,
    NonFinite,
    ByteLimit,
    NodeLimit,
    IntegerLimit,
    Resource,
}
impl fmt::Display for CodecError {
    fn fmt(&self, output: &mut fmt::Formatter<'_>) -> fmt::Result {
        output.write_str(match self {
            Self::Invalid => "outside ordered-finite-json-v2 domain",
            Self::NonFinite => "JSON floats must be finite",
            Self::ByteLimit => "JSON document exceeds 4 MiB UTF-8",
            Self::NodeLimit => "JSON node count exceeds 100000",
            Self::IntegerLimit => "JSON integer exceeds 4300 decimal digits",
            Self::Resource => "ordered JSON allocation failed",
        })
    }
}
impl Error for CodecError {}

/// A Python-JSON subtree retained until the ordered output encoder visits it.
/// Its opaque strings and keys may contain Python's non-scalar code points;
/// they are private and cannot enter `PyValue`'s public scalar-string domain.
pub struct PythonJson(PythonJsonNode);

enum PythonJsonNode {
    Native(PyValue),
    // In this private tree only, literal backslashes are doubled and lone
    // surrogates are represented by canonical `\uXXXX` tokens. This encoding
    // is injective, including for keys, so dict replacement uses Python's
    // decoded-code-point equality without substituting a Unicode scalar.
    Opaque(PyValue),
    Array(Vec<PythonJson>),
    Object(Vec<(String, PythonJson)>),
}

impl fmt::Debug for PythonJson {
    fn fmt(&self, output: &mut fmt::Formatter<'_>) -> fmt::Result {
        output.write_str("PythonJson(..)")
    }
}

impl PythonJson {
    pub fn array(values: Vec<Self>) -> Self {
        Self(PythonJsonNode::Array(values))
    }

    pub fn object(entries: impl IntoIterator<Item = (String, Self)>) -> Self {
        let mut output: Vec<(String, Self)> = Vec::new();
        for (key, value) in entries {
            if let Some((_, current)) = output.iter_mut().find(|(name, _)| name == &key) {
                *current = value;
            } else {
                output.push((key, value));
            }
        }
        Self(PythonJsonNode::Object(output))
    }

    pub fn is_object(&self) -> bool {
        match &self.0 {
            PythonJsonNode::Native(value) | PythonJsonNode::Opaque(value) => {
                value.as_object().is_some()
            }
            PythonJsonNode::Object(_) => true,
            PythonJsonNode::Array(_) => false,
        }
    }
}

impl From<PyValue> for PythonJson {
    fn from(value: PyValue) -> Self {
        Self(PythonJsonNode::Native(value))
    }
}

impl Drop for PythonJson {
    fn drop(&mut self) {
        fn children(value: &mut PythonJson, pending: &mut Vec<PythonJson>) {
            match &mut value.0 {
                PythonJsonNode::Array(values) => {
                    if pending.is_empty() {
                        std::mem::swap(pending, values);
                    } else {
                        pending.append(values);
                    }
                }
                PythonJsonNode::Object(entries) => {
                    pending.extend(entries.drain(..).map(|(_, value)| value));
                }
                PythonJsonNode::Native(_) | PythonJsonNode::Opaque(_) => {}
            }
        }
        let mut pending = Vec::new();
        children(self, &mut pending);
        while let Some(mut value) = pending.pop() {
            children(&mut value, &mut pending);
        }
    }
}

fn opaque_push(output: &mut String, text: &str) -> Result<(), CodecError> {
    output
        .try_reserve(text.len())
        .map_err(|_| CodecError::Resource)?;
    output.push_str(text);
    Ok(())
}

fn opaque_scalar(output: &mut String, value: u32) -> Result<(), CodecError> {
    if (0xd800..=0xdfff).contains(&value) {
        const HEX: &[u8; 16] = b"0123456789abcdef";
        output.try_reserve(6).map_err(|_| CodecError::Resource)?;
        output.push_str("\\u");
        for shift in [12, 8, 4, 0] {
            output.push(char::from(HEX[((value >> shift) & 0xf) as usize]));
        }
    } else {
        let value = char::from_u32(value).ok_or(CodecError::Invalid)?;
        if value == '\\' {
            opaque_push(output, "\\\\")?;
        } else {
            output
                .try_reserve(value.len_utf8())
                .map_err(|_| CodecError::Resource)?;
            output.push(value);
        }
    }
    Ok(())
}

fn hex_quad(text: &str) -> Result<u32, CodecError> {
    u32::from_str_radix(text, 16).map_err(|_| CodecError::Invalid)
}

/// Decode the ordinary json.loads string domain, including unpaired UTF-16
/// escapes. RawValue checks JSON syntax without requiring Unicode scalars.
fn opaque_string(token: &str) -> Result<String, CodecError> {
    serde_json::from_str::<&RawValue>(token).map_err(|_| CodecError::Invalid)?;
    let text = &token[1..token.len() - 1];
    let mut output = String::new();
    let mut position = 0;
    while position < text.len() {
        let character = text[position..].chars().next().ok_or(CodecError::Invalid)?;
        position += character.len_utf8();
        if character != '\\' {
            opaque_scalar(&mut output, character as u32)?;
            continue;
        }
        let escape = *text.as_bytes().get(position).ok_or(CodecError::Invalid)?;
        position += 1;
        let value = match escape {
            b'"' => '"' as u32,
            b'\\' => '\\' as u32,
            b'/' => '/' as u32,
            b'b' => 8,
            b'f' => 12,
            b'n' => '\n' as u32,
            b'r' => '\r' as u32,
            b't' => '\t' as u32,
            b'u' => {
                let quad = text
                    .get(position..position + 4)
                    .ok_or(CodecError::Invalid)?;
                let mut value = hex_quad(quad)?;
                position += 4;
                if (0xd800..=0xdbff).contains(&value)
                    && text.as_bytes()[position..].starts_with(b"\\u")
                {
                    let low = text
                        .get(position + 2..position + 6)
                        .ok_or(CodecError::Invalid)
                        .and_then(hex_quad)?;
                    if (0xdc00..=0xdfff).contains(&low) {
                        value = 0x10000 + ((value - 0xd800) << 10) + low - 0xdc00;
                        position += 6;
                    }
                }
                value
            }
            _ => return Err(CodecError::Invalid),
        };
        opaque_scalar(&mut output, value)?;
    }
    Ok(output)
}

pub(crate) fn float_repr(value: f64) -> String {
    if value.is_nan() {
        return "nan".into();
    }
    if value.is_infinite() {
        return if value.is_sign_negative() {
            "-inf"
        } else {
            "inf"
        }
        .into();
    }
    // Use the already tested Core primitive for one finite scalar. It has no
    // object-order, nesting or integer policy to apply to this pack-owned tree.
    String::from_utf8(
        tobkiri_canon::encode(&CanonValue::Float(value), Variant::LegacyFloat)
            .expect("finite scalar is inside the LegacyFloat domain"),
    )
    .expect("canonical finite float spelling is ASCII")
}

fn visit(nodes: &mut usize) -> Result<(), CodecError> {
    *nodes += 1;
    if *nodes > MAX_NODES {
        Err(CodecError::NodeLimit)
    } else {
        Ok(())
    }
}

fn integer(token: &str) -> Result<String, CodecError> {
    let digits = token.strip_prefix('-').unwrap_or(token);
    if digits.len() > MAX_INTEGER_DIGITS {
        return Err(CodecError::IntegerLimit);
    }
    if digits.is_empty()
        || !digits.bytes().all(|byte| byte.is_ascii_digit())
        || (digits.len() > 1 && digits.starts_with('0'))
    {
        return Err(CodecError::Invalid);
    }
    Ok(if token == "-0" { "0" } else { token }.into())
}

#[derive(Clone, Copy)]
enum Phase {
    FirstValue,
    Value,
    FirstKey,
    Key,
    Colon,
    Separator,
}
enum Container {
    Array(Vec<PyValue>),
    Object {
        entries: Vec<(String, PyValue)>,
        keys: BTreeSet<String>,
        positions: Option<BTreeMap<String, usize>>,
        pending: Option<String>,
    },
}
struct Frame {
    contents: Container,
    phase: Phase,
}
impl Frame {
    fn end(&self) -> u8 {
        match self.contents {
            Container::Array(_) => b']',
            Container::Object { .. } => b'}',
        }
    }
    fn finish(self) -> PyValue {
        match self.contents {
            Container::Array(values) => PyValue::Array(values),
            Container::Object { entries, .. } => PyValue::Object(entries),
        }
    }
}

struct Parser<'a> {
    input: &'a str,
    position: usize,
    nodes: usize,
    python_json: bool,
}
impl Parser<'_> {
    fn visit(&mut self) -> Result<(), CodecError> {
        if self.python_json {
            Ok(())
        } else {
            visit(&mut self.nodes)
        }
    }
    fn whitespace(&mut self) {
        while self
            .input
            .as_bytes()
            .get(self.position)
            .is_some_and(|byte| matches!(byte, b' ' | b'\t' | b'\r' | b'\n'))
        {
            self.position += 1;
        }
    }
    fn peek(&self) -> Option<u8> {
        self.input.as_bytes().get(self.position).copied()
    }
    fn string(&mut self) -> Result<String, CodecError> {
        let start = self.position;
        if self.peek() != Some(b'"') {
            return Err(CodecError::Invalid);
        }
        self.position += 1;
        loop {
            match self.peek() {
                Some(b'"') => {
                    self.position += 1;
                    let token = &self.input[start..self.position];
                    if self.python_json {
                        return opaque_string(token);
                    }
                    return serde_json::from_str(token).map_err(|_| CodecError::Invalid);
                }
                Some(b'\\') => {
                    self.position += 1;
                    if self.peek().is_none() {
                        return Err(CodecError::Invalid);
                    }
                    self.position += 1;
                }
                Some(_) => self.position += 1,
                None => return Err(CodecError::Invalid),
            }
        }
    }
    fn scalar(&mut self) -> Result<PyValue, CodecError> {
        if self.peek() == Some(b'"') {
            return self.string().map(PyValue::String);
        }
        let start = self.position;
        while self.peek().is_some_and(|byte| {
            !matches!(
                byte,
                b' ' | b'\t' | b'\r' | b'\n' | b',' | b':' | b'[' | b']' | b'{' | b'}'
            )
        }) {
            self.position += 1;
        }
        let token = &self.input[start..self.position];
        match token {
            "null" => return Ok(PyValue::Null),
            "true" => return Ok(PyValue::Bool(true)),
            "false" => return Ok(PyValue::Bool(false)),
            "NaN" | "Infinity" | "-Infinity" => {
                return if self.python_json {
                    Ok(PyValue::Float(match token {
                        "NaN" => f64::NAN,
                        "Infinity" => f64::INFINITY,
                        _ => f64::NEG_INFINITY,
                    }))
                } else {
                    Err(CodecError::NonFinite)
                };
            }
            _ => {}
        }
        // RawValue validates JSON number grammar without converting arbitrary
        // integers to a private serde_json Number map or binary64 first.
        serde_json::from_str::<&RawValue>(token).map_err(|_| CodecError::Invalid)?;
        if !matches!(token.as_bytes().first(), Some(b'-' | b'0'..=b'9')) {
            return Err(CodecError::Invalid);
        }
        if token.bytes().any(|byte| matches!(byte, b'.' | b'e' | b'E')) {
            let value = token.parse::<f64>().map_err(|_| CodecError::Invalid)?;
            if !value.is_finite() && !self.python_json {
                return Err(CodecError::NonFinite);
            }
            Ok(PyValue::Float(value))
        } else {
            integer(token).map(PyValue::Int)
        }
    }
}

fn attach(
    value: PyValue,
    frames: &mut [Frame],
    root: &mut Option<PyValue>,
) -> Result<(), CodecError> {
    if let Some(frame) = frames.last_mut() {
        match &mut frame.contents {
            Container::Array(values) => {
                values.try_reserve(1).map_err(|_| CodecError::Resource)?;
                values.push(value);
            }
            Container::Object {
                entries,
                positions,
                pending,
                ..
            } => {
                let key = pending.take().expect("object value has a decoded key");
                if let Some(index) = positions
                    .as_ref()
                    .and_then(|positions| positions.get(&key))
                    .copied()
                {
                    entries[index].1 = value;
                    frame.phase = Phase::Separator;
                    return Ok(());
                }
                entries.try_reserve(1).map_err(|_| CodecError::Resource)?;
                if let Some(positions) = positions {
                    positions.insert(key.clone(), entries.len());
                }
                entries.push((key, value));
            }
        }
        frame.phase = Phase::Separator;
    } else {
        *root = Some(value);
    }
    Ok(())
}

fn parse_document(input: &str, python_json: bool) -> Result<PyValue, CodecError> {
    if input.len() > MAX_BYTES {
        return Err(CodecError::ByteLimit);
    }
    let mut parser = Parser {
        input,
        position: 0,
        nodes: 0,
        python_json,
    };
    let mut frames: Vec<Frame> = Vec::new();
    let mut root = None;
    loop {
        parser.whitespace();
        if frames.is_empty()
            && let Some(value) = root.take()
        {
            return if parser.position == input.len() {
                Ok(value)
            } else {
                Err(CodecError::Invalid)
            };
        }
        let phase = frames.last().map_or(Phase::Value, |frame| frame.phase);
        if matches!(
            phase,
            Phase::Separator | Phase::FirstValue | Phase::FirstKey
        ) && frames
            .last()
            .is_some_and(|frame| parser.peek() == Some(frame.end()))
        {
            parser.position += 1;
            let value = frames.pop().expect("matching close has a frame").finish();
            attach(value, &mut frames, &mut root)?;
            continue;
        }
        match phase {
            Phase::Separator => {
                if parser.peek() != Some(b',') {
                    return Err(CodecError::Invalid);
                }
                parser.position += 1;
                let frame = frames.last_mut().expect("separator is inside a frame");
                frame.phase = match frame.contents {
                    Container::Array(_) => Phase::Value,
                    Container::Object { .. } => Phase::Key,
                };
            }
            Phase::FirstKey | Phase::Key => {
                parser.visit()?;
                let key = parser.string()?;
                let frame = frames.last_mut().expect("key is inside an object");
                if let Container::Object {
                    keys,
                    positions,
                    pending,
                    ..
                } = &mut frame.contents
                {
                    if positions.is_none() && !keys.insert(key.clone()) {
                        return Err(CodecError::Invalid);
                    }
                    *pending = Some(key);
                }
                frame.phase = Phase::Colon;
            }
            Phase::Colon => {
                if parser.peek() != Some(b':') {
                    return Err(CodecError::Invalid);
                }
                parser.position += 1;
                frames.last_mut().expect("colon is inside an object").phase = Phase::Value;
            }
            Phase::FirstValue | Phase::Value => {
                parser.visit()?;
                let contents = match parser.peek() {
                    Some(b'[') => Some((Container::Array(Vec::new()), Phase::FirstValue)),
                    Some(b'{') => Some((
                        Container::Object {
                            entries: Vec::new(),
                            keys: BTreeSet::new(),
                            positions: python_json.then(BTreeMap::new),
                            pending: None,
                        },
                        Phase::FirstKey,
                    )),
                    _ => None,
                };
                if let Some((contents, phase)) = contents {
                    parser.position += 1;
                    frames.try_reserve(1).map_err(|_| CodecError::Resource)?;
                    frames.push(Frame { contents, phase });
                } else {
                    let value = parser.scalar()?;
                    attach(value, &mut frames, &mut root)?;
                }
            }
        }
    }
}

/// Decode the v2 native JSON domain. Every decoded key occurrence counts as a
/// node, and duplicate decoded keys are rejected as in the canonical Python v2.
/// Traversal has byte/node budgets and no additional fixed nesting policy.
pub fn loads(input: &str) -> Result<PyValue, CodecError> {
    parse_document(input, false)
}

/// Parse an opaque argument string already enclosed by bounded ordered DATA.
/// Python JSON retains duplicate-last-value at the first key position and may
/// contain NaN/Infinity and lone surrogates. Neither is rejected before final
/// ordered output traversal. There is no added node or nesting cap here; the
/// enclosing string proves the byte envelope. Syntax/integer errors and real
/// allocation failures still occur during parsing, as in the original stage.
pub fn loads_python_json(input: &str) -> Result<PythonJson, CodecError> {
    parse_document(input, true).map(|value| PythonJson(PythonJsonNode::Opaque(value)))
}

enum Part<'a> {
    Value(&'a PyValue, bool),
    Key(&'a str, bool),
    PythonJson(&'a PythonJson),
    Text(&'static str),
}

fn append(output: &mut String, text: &str) -> Result<(), CodecError> {
    if text.len() > MAX_BYTES - output.len() {
        return Err(CodecError::ByteLimit);
    }
    output
        .try_reserve(text.len())
        .map_err(|_| CodecError::Resource)?;
    output.push_str(text);
    Ok(())
}

struct StringSink<'a> {
    output: &'a mut String,
    failure: Option<CodecError>,
}
impl io::Write for StringSink<'_> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        // serde_json's string formatter writes valid UTF-8 fragments or ASCII
        // escapes. Its authoritative serializer still chooses every escape.
        let text = std::str::from_utf8(bytes).map_err(io::Error::other)?;
        if let Err(error) = append(self.output, text) {
            self.failure = Some(error);
            return Err(io::Error::other("bounded JSON string output failed"));
        }
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn string(output: &mut String, value: &str) -> Result<(), CodecError> {
    let mut sink = StringSink {
        output,
        failure: None,
    };
    serde_json::to_writer(&mut sink, value).map_err(|_| sink.failure.unwrap_or(CodecError::Invalid))
}

fn opaque_output_string(output: &mut String, value: &str) -> Result<(), CodecError> {
    let mut decoded = String::new();
    let mut characters = value.chars();
    while let Some(character) = characters.next() {
        if character == '\\' && characters.next() != Some('\\') {
            // Only an actual lone-surrogate token can take this path. A
            // literal backslash from Python's string was encoded twice.
            return Err(CodecError::Invalid);
        }
        decoded
            .try_reserve(character.len_utf8())
            .map_err(|_| CodecError::Resource)?;
        decoded.push(character);
    }
    string(output, &decoded)
}

/// Encode ordered values with CPython's finite float spelling and UTF-8 JSON
/// string escaping. This uses an explicit traversal stack, as does Python v2.
pub fn dumps(value: &PyValue) -> Result<String, CodecError> {
    encode(Part::Value(value, false))
}

/// Encode mixed native and opaque Python-JSON subtrees in one ordered traversal.
/// Surrogate, nonfinite, byte and node failures are selected only when their
/// original output position is visited, after provider normalization succeeds.
pub fn dumps_python_json(value: &PythonJson) -> Result<String, CodecError> {
    encode(Part::PythonJson(value))
}

fn encode(first: Part<'_>) -> Result<String, CodecError> {
    let mut output = String::new();
    let mut parts = Vec::new();
    parts.try_reserve(1).map_err(|_| CodecError::Resource)?;
    parts.push(first);
    let mut nodes = 0;
    while let Some(part) = parts.pop() {
        match part {
            Part::Text(text) => append(&mut output, text)?,
            Part::Key(key, opaque) => {
                visit(&mut nodes)?;
                if opaque {
                    opaque_output_string(&mut output, key)?;
                } else {
                    string(&mut output, key)?;
                }
            }
            Part::PythonJson(value) => match &value.0 {
                PythonJsonNode::Native(value) => parts.push(Part::Value(value, false)),
                PythonJsonNode::Opaque(value) => parts.push(Part::Value(value, true)),
                PythonJsonNode::Array(values) => {
                    visit(&mut nodes)?;
                    if values.len() > MAX_NODES - nodes {
                        return Err(CodecError::NodeLimit);
                    }
                    parts
                        .try_reserve(2 * values.len() + 1)
                        .map_err(|_| CodecError::Resource)?;
                    append(&mut output, "[")?;
                    parts.push(Part::Text("]"));
                    for (index, value) in values.iter().enumerate().rev() {
                        parts.push(Part::PythonJson(value));
                        if index != 0 {
                            parts.push(Part::Text(","));
                        }
                    }
                }
                PythonJsonNode::Object(entries) => {
                    visit(&mut nodes)?;
                    if entries.len() > (MAX_NODES - nodes) / 2 {
                        return Err(CodecError::NodeLimit);
                    }
                    parts
                        .try_reserve(4 * entries.len() + 1)
                        .map_err(|_| CodecError::Resource)?;
                    append(&mut output, "{")?;
                    parts.push(Part::Text("}"));
                    for (index, (key, value)) in entries.iter().enumerate().rev() {
                        parts.push(Part::PythonJson(value));
                        parts.push(Part::Text(":"));
                        parts.push(Part::Key(key, false));
                        if index != 0 {
                            parts.push(Part::Text(","));
                        }
                    }
                }
            },
            Part::Value(value, opaque) => {
                visit(&mut nodes)?;
                match value {
                    PyValue::Null => append(&mut output, "null")?,
                    PyValue::Bool(value) => {
                        append(&mut output, if *value { "true" } else { "false" })?
                    }
                    PyValue::Int(value) => append(&mut output, &integer(value)?)?,
                    PyValue::Float(value) => {
                        if !value.is_finite() {
                            return Err(CodecError::NonFinite);
                        }
                        append(&mut output, &float_repr(*value))?;
                    }
                    PyValue::String(value) => {
                        if opaque {
                            opaque_output_string(&mut output, value)?;
                        } else {
                            string(&mut output, value)?;
                        }
                    }
                    PyValue::Array(values) => {
                        if values.len() > MAX_NODES - nodes {
                            return Err(CodecError::NodeLimit);
                        }
                        parts
                            .try_reserve(2 * values.len() + 1)
                            .map_err(|_| CodecError::Resource)?;
                        append(&mut output, "[")?;
                        parts.push(Part::Text("]"));
                        for (index, value) in values.iter().enumerate().rev() {
                            parts.push(Part::Value(value, opaque));
                            if index != 0 {
                                parts.push(Part::Text(","));
                            }
                        }
                    }
                    PyValue::Object(entries) => {
                        if entries.len() > (MAX_NODES - nodes) / 2 {
                            return Err(CodecError::NodeLimit);
                        }
                        parts
                            .try_reserve(4 * entries.len() + 1)
                            .map_err(|_| CodecError::Resource)?;
                        let mut keys = BTreeSet::new();
                        if entries.iter().any(|(key, _)| !keys.insert(key)) {
                            return Err(CodecError::Invalid);
                        }
                        append(&mut output, "{")?;
                        parts.push(Part::Text("}"));
                        for (index, (key, value)) in entries.iter().enumerate().rev() {
                            parts.push(Part::Value(value, opaque));
                            parts.push(Part::Text(":"));
                            parts.push(Part::Key(key, opaque));
                            if index != 0 {
                                parts.push(Part::Text(","));
                            }
                        }
                    }
                }
            }
        }
    }
    Ok(output)
}
