use crate::{CanonValue, Error, MAX_BYTES, MAX_DEPTH, Variant};
use std::collections::BTreeMap;

fn invalid(message: &str) -> Error {
    Error::Invalid(message.into())
}

pub(crate) fn parse(input: &[u8], variant: Variant) -> Result<CanonValue, Error> {
    if variant == Variant::StrictV1 && input.len() > MAX_BYTES {
        return Err(Error::Limit("4 MiB"));
    }
    std::str::from_utf8(input).map_err(|_| invalid("input is not UTF-8"))?;
    let mut parser = Parser {
        input,
        at: 0,
        variant,
    };
    let value = parser.value(0)?;
    parser.whitespace();
    if parser.at != input.len() {
        return Err(invalid("trailing JSON data"));
    }
    Ok(value)
}

struct Parser<'a> {
    input: &'a [u8],
    at: usize,
    variant: Variant,
}
impl Parser<'_> {
    fn whitespace(&mut self) {
        while self
            .input
            .get(self.at)
            .is_some_and(|b| matches!(b, b' ' | b'\n' | b'\r' | b'\t'))
        {
            self.at += 1;
        }
    }
    fn byte(&mut self, wanted: u8) -> Result<(), Error> {
        if self.input.get(self.at) != Some(&wanted) {
            return Err(invalid("unexpected JSON token"));
        }
        self.at += 1;
        Ok(())
    }
    fn literal(&mut self, token: &[u8], value: CanonValue) -> Result<CanonValue, Error> {
        if !self.input[self.at..].starts_with(token) {
            return Err(invalid("invalid JSON literal"));
        }
        self.at += token.len();
        Ok(value)
    }
    fn value(&mut self, depth: usize) -> Result<CanonValue, Error> {
        let maximum = if self.variant == Variant::StrictV1 {
            MAX_DEPTH
        } else {
            1024
        };
        if depth > maximum {
            return Err(Error::Limit("nesting depth"));
        }
        self.whitespace();
        match self.input.get(self.at).copied() {
            Some(b'n') => self.literal(b"null", CanonValue::Null),
            Some(b't') => self.literal(b"true", CanonValue::Bool(true)),
            Some(b'f') => self.literal(b"false", CanonValue::Bool(false)),
            Some(b'"') => Ok(CanonValue::Str(self.string()?)),
            Some(b'[') => {
                self.at += 1;
                self.whitespace();
                let mut values = Vec::new();
                if self.input.get(self.at) == Some(&b']') {
                    self.at += 1;
                    return Ok(CanonValue::Arr(values));
                }
                loop {
                    values.push(self.value(depth + 1)?);
                    self.whitespace();
                    if self.input.get(self.at) == Some(&b']') {
                        self.at += 1;
                        break;
                    }
                    self.byte(b',')?;
                }
                Ok(CanonValue::Arr(values))
            }
            Some(b'{') => {
                self.at += 1;
                self.whitespace();
                let mut values = BTreeMap::new();
                if self.input.get(self.at) == Some(&b'}') {
                    self.at += 1;
                    return Ok(CanonValue::Obj(values));
                }
                loop {
                    self.whitespace();
                    let key = self.string()?;
                    if self.variant == Variant::StrictV1 && values.contains_key(&key) {
                        return Err(invalid("duplicate JSON key"));
                    }
                    self.whitespace();
                    self.byte(b':')?;
                    let value = self.value(depth + 1)?;
                    values.insert(key, value);
                    self.whitespace();
                    if self.input.get(self.at) == Some(&b'}') {
                        self.at += 1;
                        break;
                    }
                    self.byte(b',')?;
                }
                Ok(CanonValue::Obj(values))
            }
            Some(b'N') if self.variant != Variant::StrictV1 => {
                self.literal(b"NaN", CanonValue::Float(f64::NAN))
            }
            Some(b'I') if self.variant != Variant::StrictV1 => {
                self.literal(b"Infinity", CanonValue::Float(f64::INFINITY))
            }
            Some(b'-')
                if self.variant != Variant::StrictV1
                    && self.input[self.at..].starts_with(b"-Infinity") =>
            {
                self.literal(b"-Infinity", CanonValue::Float(f64::NEG_INFINITY))
            }
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => Err(invalid("invalid JSON token")),
        }
    }
    fn number(&mut self) -> Result<CanonValue, Error> {
        let start = self.at;
        if self.input.get(self.at) == Some(&b'-') {
            self.at += 1;
        }
        match self.input.get(self.at) {
            Some(b'0') => {
                self.at += 1;
            }
            Some(b'1'..=b'9') => {
                while self.input.get(self.at).is_some_and(u8::is_ascii_digit) {
                    self.at += 1;
                }
            }
            _ => return Err(invalid("invalid JSON number")),
        }
        let mut floating = false;
        if self.input.get(self.at) == Some(&b'.') {
            floating = true;
            self.at += 1;
            let first = self.at;
            while self.input.get(self.at).is_some_and(u8::is_ascii_digit) {
                self.at += 1;
            }
            if self.at == first {
                return Err(invalid("missing fractional digits"));
            }
        }
        if self
            .input
            .get(self.at)
            .is_some_and(|b| matches!(b, b'e' | b'E'))
        {
            floating = true;
            self.at += 1;
            if self
                .input
                .get(self.at)
                .is_some_and(|b| matches!(b, b'+' | b'-'))
            {
                self.at += 1;
            }
            let first = self.at;
            while self.input.get(self.at).is_some_and(u8::is_ascii_digit) {
                self.at += 1;
            }
            if self.at == first {
                return Err(invalid("missing exponent digits"));
            }
        }
        let text = std::str::from_utf8(&self.input[start..self.at])
            .map_err(|_| invalid("invalid number"))?;
        if floating {
            if self.variant == Variant::StrictV1 {
                return Err(invalid("floating point values are not permitted"));
            }
            return Ok(CanonValue::Float(
                text.parse().map_err(|_| invalid("invalid float"))?,
            ));
        }
        let value = match text.parse::<i64>() {
            Ok(value) => CanonValue::Int(value),
            Err(_) => CanonValue::BigInt(text.into()),
        };
        if self.variant == Variant::StrictV1 {
            validate_integer(&value, true)?;
        }
        Ok(value)
    }
    fn hex4(&mut self) -> Result<u16, Error> {
        let mut result = 0_u16;
        for _ in 0..4 {
            let byte = *self
                .input
                .get(self.at)
                .ok_or_else(|| invalid("truncated Unicode escape"))?;
            self.at += 1;
            let digit = match byte {
                b'0'..=b'9' => byte - b'0',
                b'a'..=b'f' => byte - b'a' + 10,
                b'A'..=b'F' => byte - b'A' + 10,
                _ => return Err(invalid("invalid Unicode escape")),
            };
            result = result * 16 + u16::from(digit);
        }
        Ok(result)
    }
    fn string(&mut self) -> Result<String, Error> {
        self.byte(b'"')?;
        let mut output = Vec::new();
        loop {
            let byte = *self
                .input
                .get(self.at)
                .ok_or_else(|| invalid("unterminated JSON string"))?;
            self.at += 1;
            match byte {
                b'"' => {
                    return String::from_utf8(output)
                        .map_err(|_| invalid("invalid Unicode string"));
                }
                0..=0x1f => return Err(invalid("unescaped control character")),
                b'\\' => {
                    let escaped = *self
                        .input
                        .get(self.at)
                        .ok_or_else(|| invalid("truncated escape"))?;
                    self.at += 1;
                    match escaped {
                        b'"' | b'\\' | b'/' => output.push(escaped),
                        b'b' => output.push(8),
                        b'f' => output.push(12),
                        b'n' => output.push(10),
                        b'r' => output.push(13),
                        b't' => output.push(9),
                        b'u' => {
                            let first = self.hex4()?;
                            let code = if (0xd800..=0xdbff).contains(&first) {
                                self.byte(b'\\')?;
                                self.byte(b'u')?;
                                let last = self.hex4()?;
                                if !(0xdc00..=0xdfff).contains(&last) {
                                    return Err(invalid("unpaired surrogate"));
                                }
                                0x10000 + ((u32::from(first) - 0xd800) << 10) + u32::from(last)
                                    - 0xdc00
                            } else if (0xdc00..=0xdfff).contains(&first) {
                                return Err(invalid("unpaired surrogate"));
                            } else {
                                u32::from(first)
                            };
                            let character = char::from_u32(code)
                                .ok_or_else(|| invalid("invalid Unicode scalar"))?;
                            let mut buffer = [0; 4];
                            output.extend_from_slice(character.encode_utf8(&mut buffer).as_bytes());
                        }
                        _ => return Err(invalid("invalid JSON string escape")),
                    }
                }
                _ => output.push(byte),
            }
        }
    }
}

fn validate_integer(value: &CanonValue, strict: bool) -> Result<String, Error> {
    let text = match value {
        CanonValue::Int(value) => value.to_string(),
        CanonValue::BigInt(value) => value.clone(),
        _ => return Err(invalid("not an integer")),
    };
    let digits = text.strip_prefix('-').unwrap_or(&text);
    if digits.is_empty()
        || !digits.bytes().all(|b| b.is_ascii_digit())
        || digits.len() > 1 && digits.starts_with('0')
    {
        return Err(invalid("invalid integer digits"));
    }
    if strict && (digits.len() > 16 || digits.len() == 16 && digits > "9007199254740991") {
        return Err(invalid("integer exceeds I-JSON safe range"));
    }
    if digits == "0" {
        Ok("0".into())
    } else {
        Ok(text)
    }
}

fn string_bytes(value: &str, output: &mut Vec<u8>) {
    output.push(b'"');
    for character in value.chars() {
        match character {
            '"' => output.extend_from_slice(b"\\\""),
            '\\' => output.extend_from_slice(b"\\\\"),
            '\n' => output.extend_from_slice(b"\\n"),
            '\r' => output.extend_from_slice(b"\\r"),
            '\t' => output.extend_from_slice(b"\\t"),
            '\u{8}' => output.extend_from_slice(b"\\b"),
            '\u{c}' => output.extend_from_slice(b"\\f"),
            '\u{0}'..='\u{1f}' => {
                output.extend_from_slice(format!("\\u{:04x}", character as u32).as_bytes())
            }
            _ => {
                let mut buffer = [0; 4];
                output.extend_from_slice(character.encode_utf8(&mut buffer).as_bytes());
            }
        }
    }
    output.push(b'"');
}

/// Adapt shortest-round-trip digits to Python repr's exponent and decimal rules.
fn python_float(value: f64, allow_nonfinite: bool) -> Result<String, Error> {
    if !value.is_finite() {
        if !allow_nonfinite {
            return Err(invalid("non-finite numbers are not permitted"));
        }
        return Ok(if value.is_nan() {
            "NaN"
        } else if value.is_sign_negative() {
            "-Infinity"
        } else {
            "Infinity"
        }
        .into());
    }
    if value == 0.0 {
        return Ok(if value.is_sign_negative() {
            "-0.0"
        } else {
            "0.0"
        }
        .into());
    }
    let mut buffer = ryu::Buffer::new();
    let shortest = buffer.format_finite(value.abs());
    let (mantissa, explicit) = shortest
        .split_once('e')
        .map_or((shortest, 0), |(mantissa, exponent)| {
            (mantissa, exponent.parse::<i32>().expect("ryu exponent"))
        });
    let decimal = mantissa.find('.').unwrap_or(mantissa.len()) as i32;
    let all: String = mantissa.chars().filter(|c| *c != '.').collect();
    let first = all.bytes().position(|b| b != b'0').expect("nonzero float");
    let digits = all[first..].trim_end_matches('0');
    let exponent = explicit + decimal - first as i32 - 1;
    let mut result = if value.is_sign_negative() {
        "-".to_string()
    } else {
        String::new()
    };
    if !(-4..16).contains(&exponent) {
        result.push(digits.as_bytes()[0] as char);
        if digits.len() > 1 {
            result.push('.');
            result.push_str(&digits[1..]);
        }
        result.push('e');
        result.push(if exponent < 0 { '-' } else { '+' });
        result.push_str(&format!("{:02}", exponent.unsigned_abs()));
    } else if exponent < 0 {
        result.push_str("0.");
        result.push_str(&"0".repeat((-exponent - 1) as usize));
        result.push_str(digits);
    } else {
        let before = exponent as usize + 1;
        if digits.len() > before {
            result.push_str(&digits[..before]);
            result.push('.');
            result.push_str(&digits[before..]);
        } else {
            result.push_str(digits);
            result.push_str(&"0".repeat(before - digits.len()));
            result.push_str(".0");
        }
    }
    Ok(result)
}

pub(crate) fn encode(value: &CanonValue, variant: Variant) -> Result<Vec<u8>, Error> {
    fn visit(
        value: &CanonValue,
        variant: Variant,
        depth: usize,
        output: &mut Vec<u8>,
    ) -> Result<(), Error> {
        if depth
            > if variant == Variant::StrictV1 {
                MAX_DEPTH
            } else {
                1024
            }
        {
            return Err(Error::Limit("nesting depth"));
        }
        match value {
            CanonValue::Null => output.extend_from_slice(b"null"),
            CanonValue::Bool(value) => {
                output.extend_from_slice(if *value { b"true" } else { b"false" })
            }
            CanonValue::Int(_) | CanonValue::BigInt(_) => output.extend_from_slice(
                validate_integer(value, variant == Variant::StrictV1)?.as_bytes(),
            ),
            CanonValue::Float(value) => {
                if variant == Variant::StrictV1 {
                    return Err(invalid("floating point values are not permitted"));
                }
                output.extend_from_slice(
                    python_float(*value, variant == Variant::LegacyPackSig)?.as_bytes(),
                );
            }
            CanonValue::Str(value) => string_bytes(value, output),
            CanonValue::Arr(items) => {
                output.push(b'[');
                for (index, item) in items.iter().enumerate() {
                    if index > 0 {
                        output.push(b',');
                    }
                    visit(item, variant, depth + 1, output)?;
                }
                output.push(b']');
            }
            CanonValue::Obj(items) => {
                output.push(b'{');
                for (index, (key, value)) in items.iter().enumerate() {
                    if index > 0 {
                        output.push(b',');
                    }
                    string_bytes(key, output);
                    output.push(b':');
                    visit(value, variant, depth + 1, output)?;
                }
                output.push(b'}');
            }
        }
        if variant == Variant::StrictV1 && output.len() > MAX_BYTES {
            return Err(Error::Limit("4 MiB"));
        }
        Ok(())
    }
    let mut output = Vec::new();
    visit(value, variant, 0, &mut output)?;
    Ok(output)
}
