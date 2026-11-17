//! Verification of the one bundle identifier in an already captured Info.plist.
use super::*;
use quick_xml::{Reader, escape::resolve_xml_entity, events::Event};

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::Source;
const KEY: &str = "CFBundleIdentifier";
const INVALID: &str = "macOS packaged artifact Info.plist is invalid";
const MISMATCH: &str = "macOS packaged artifact bundle identity does not match";
const MAX_DEPTH: usize = 128;

#[derive(Debug)]
enum DecodeError {
    Invalid,
    Operation(OriginalAuthorityError),
}
type Decode<T> = std::result::Result<T, DecodeError>;

impl From<OriginalAuthorityError> for DecodeError {
    fn from(error: OriginalAuthorityError) -> Self {
        Self::Operation(error)
    }
}

fn invalid<T>(value: Option<T>) -> Decode<T> {
    value.ok_or(DecodeError::Invalid)
}

fn decoded<T, E>(value: std::result::Result<T, E>) -> Decode<T> {
    value.map_err(|_| DecodeError::Invalid)
}

/// Verify both plist encodings without acquiring any filesystem authority.
pub(super) fn check_bundle_identity<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    raw: &[u8],
    expected: &str,
) -> Result<()> {
    op.check(STAGE)?;
    let mut meter = Meter {
        op,
        budget,
        work: 0,
    };
    let result = if raw.starts_with(b"bplist00") {
        binary_identity(raw, expected, &mut meter)
    } else {
        xml_identity(raw, expected, &mut meter)
    };
    if let Err(DecodeError::Operation(error)) = result {
        return Err(error);
    }
    meter.flush()?;
    match result {
        Ok(true) => Ok(()),
        Ok(false) => Err(artifact_identity::artifact_rejected(MISMATCH)),
        Err(DecodeError::Invalid) => Err(artifact_identity::artifact_rejected(INVALID)),
        Err(DecodeError::Operation(_)) => unreachable!(),
    }
}

struct Meter<'a, 'b, S> {
    op: &'a Operation<'b, S>,
    budget: &'a Arc<dyn AllocationBudget>,
    work: u64,
}

impl<S: OriginalAuthorityOwner> Meter<'_, '_, S> {
    fn add(&mut self, bytes: usize) -> Decode<()> {
        self.work = self.work.saturating_add(bytes as u64);
        if self.work >= 64 * 1024 {
            self.flush()?;
        }
        Ok(())
    }

    fn flush(&mut self) -> Result<()> {
        self.op.check(STAGE)?;
        charge_work(self.budget, std::mem::take(&mut self.work))
    }
}

#[derive(Clone, Copy)]
enum Frame {
    Wrapper,
    Dict(Option<bool>),
    Array,
    Scalar(Scalar),
}

#[derive(Clone, Copy)]
enum Scalar {
    Key,
    String,
    Boolean,
    Integer,
    Real,
    Date,
    Data,
}

fn xml_identity<S: OriginalAuthorityOwner>(
    raw: &[u8],
    expected: &str,
    meter: &mut Meter<'_, '_, S>,
) -> Decode<bool> {
    let capacity = raw
        .len()
        .checked_mul(12)
        .and_then(|bytes| bytes.checked_add(16 * 1024))
        .ok_or_else(|| OriginalAuthorityError::capacity(STAGE))?;
    // Covers the UTF-16 conversion, quick-xml's open tag stack, one scalar
    // and temporary attribute/text decoding. No DOM or value tree is built.
    let _capacity = meter.op.reserve(STAGE, capacity)?;
    meter.add(raw.len().saturating_mul(4))?;
    let converted;
    let (input, utf16) = if raw.starts_with(b"\xfe\xff") || raw.starts_with(b"\xff\xfe") {
        let little = raw[0] == 0xff;
        let body = &raw[2..];
        if !body.len().is_multiple_of(2) {
            return Err(DecodeError::Invalid);
        }
        let units = body.chunks_exact(2).map(|b| {
            let pair = [b[0], b[1]];
            if little {
                u16::from_le_bytes(pair)
            } else {
                u16::from_be_bytes(pair)
            }
        });
        let mut out = String::with_capacity(body.len() / 2 * 3);
        for ch in char::decode_utf16(units) {
            out.push(decoded(ch)?);
        }
        converted = out;
        (converted.as_bytes(), true)
    } else {
        (raw.strip_prefix(b"\xef\xbb\xbf").unwrap_or(raw), false)
    };
    if !(input.starts_with(b"<?xml") || input.starts_with(b"<plist")) {
        return Err(DecodeError::Invalid);
    }
    if !decoded(std::str::from_utf8(input))?
        .chars()
        .all(valid_xml_char)
    {
        return Err(DecodeError::Invalid);
    }

    let mut xml = Reader::from_reader(input);
    let config = xml.config_mut();
    config.check_comments = true;
    config.expand_empty_elements = true;
    let mut stack = Vec::with_capacity(MAX_DEPTH);
    let mut scalar = String::new();
    let mut root_dict = None;
    let mut root_depth = 0;
    let mut root_opened = false;
    let mut identity_matches = false;
    let mut selected_string = false;
    let mut seen_doctype = false;
    let mut seen_decl = false;
    loop {
        meter.add(32)?;
        match decoded(xml.read_event())? {
            Event::Start(tag) => {
                if stack.len() == MAX_DEPTH || (root_opened && stack.is_empty()) {
                    return Err(DecodeError::Invalid);
                }
                root_opened = true;
                for attr in tag.attributes() {
                    let attr = decoded(attr)?;
                    if attr.value.contains(&b'<') {
                        return Err(DecodeError::Invalid);
                    }
                    decoded(attr.decode_and_unescape_value(xml.decoder()))?;
                }
                let frame = match tag.name().as_ref() {
                    b"plist" if stack.is_empty() => Frame::Wrapper,
                    b"dict" => Frame::Dict(None),
                    b"array" => Frame::Array,
                    b"key" => Frame::Scalar(Scalar::Key),
                    b"string" => Frame::Scalar(Scalar::String),
                    b"true" | b"false" => Frame::Scalar(Scalar::Boolean),
                    b"integer" => Frame::Scalar(Scalar::Integer),
                    b"real" => Frame::Scalar(Scalar::Real),
                    b"date" => Frame::Scalar(Scalar::Date),
                    b"data" => Frame::Scalar(Scalar::Data),
                    _ => return Err(DecodeError::Invalid),
                };
                if matches!(frame, Frame::Scalar(Scalar::Key)) {
                    if !matches!(stack.last(), Some(Frame::Dict(None))) {
                        return Err(DecodeError::Invalid);
                    }
                } else if !matches!(frame, Frame::Wrapper) {
                    let depth = stack.len();
                    match stack.last_mut() {
                        None | Some(Frame::Wrapper) => {
                            if root_dict.replace(matches!(frame, Frame::Dict(_))).is_some() {
                                return Err(DecodeError::Invalid);
                            }
                            root_depth = depth + 1;
                        }
                        Some(Frame::Dict(slot)) => {
                            let selected = invalid(slot.take())?;
                            selected_string = depth == root_depth
                                && selected
                                && matches!(frame, Frame::Scalar(Scalar::String));
                            if depth == root_depth && selected {
                                identity_matches = false;
                            }
                        }
                        Some(Frame::Array) => {}
                        Some(Frame::Scalar(_)) => return Err(DecodeError::Invalid),
                    }
                }
                if matches!(frame, Frame::Scalar(_)) {
                    scalar.clear();
                }
                stack.push(frame);
            }
            Event::End(_) => {
                let frame = invalid(stack.pop())?;
                match frame {
                    Frame::Dict(Some(_)) => return Err(DecodeError::Invalid),
                    Frame::Scalar(Scalar::Key) => {
                        let Some(Frame::Dict(slot)) = stack.last_mut() else {
                            return Err(DecodeError::Invalid);
                        };
                        *slot = Some(scalar == KEY);
                    }
                    Frame::Scalar(Scalar::String) => {
                        if selected_string {
                            identity_matches = scalar == expected;
                        }
                    }
                    Frame::Scalar(kind) if !valid_xml_scalar(kind, &scalar) => {
                        return Err(DecodeError::Invalid);
                    }
                    _ => {}
                }
            }
            Event::Text(body) => {
                let text = decoded(body.xml10_content())?;
                if text.contains("]]>")
                    || (stack.is_empty() && !text.bytes().all(|b| b.is_ascii_whitespace()))
                {
                    return Err(DecodeError::Invalid);
                }
                if matches!(stack.last(), Some(Frame::Scalar(_))) {
                    scalar.push_str(&text);
                }
            }
            Event::CData(body) if !stack.is_empty() => {
                let text = decoded(body.xml10_content())?;
                if matches!(stack.last(), Some(Frame::Scalar(_))) {
                    scalar.push_str(&text);
                }
            }
            Event::GeneralRef(reference) if !stack.is_empty() => {
                let name = decoded(reference.decode())?;
                if let Some(ch) = decoded(reference.resolve_char_ref())? {
                    if !valid_xml_char(ch) {
                        return Err(DecodeError::Invalid);
                    }
                    if matches!(stack.last(), Some(Frame::Scalar(_))) {
                        scalar.push(ch);
                    }
                } else {
                    let entity = invalid(resolve_xml_entity(&name))?;
                    if matches!(stack.last(), Some(Frame::Scalar(_))) {
                        scalar.push_str(entity);
                    }
                }
            }
            Event::Decl(decl) if !root_opened && !seen_doctype && !seen_decl => {
                seen_decl = true;
                let version = decoded(decl.version())?;
                if version.as_ref() != b"1.0" {
                    return Err(DecodeError::Invalid);
                }
                if let Some(encoding) = decl.encoding() {
                    let encoding = decoded(encoding)?;
                    let supported = if utf16 {
                        [b"UTF-16".as_slice(), b"UTF-16LE", b"UTF-16BE"]
                            .iter()
                            .any(|name| encoding.eq_ignore_ascii_case(name))
                    } else {
                        encoding.eq_ignore_ascii_case(b"UTF-8")
                            || (encoding.eq_ignore_ascii_case(b"US-ASCII") && input.is_ascii())
                    };
                    if !supported {
                        return Err(DecodeError::Invalid);
                    }
                }
            }
            Event::DocType(body) if !root_opened && !seen_doctype => {
                // Expanding or resolving user-defined entities is never part of
                // plistlib's Info.plist input format. The usual external Apple
                // PUBLIC identifier is accepted but is never opened.
                if body.contains(&b'[') {
                    return Err(DecodeError::Invalid);
                }
                seen_doctype = true;
            }
            Event::Comment(_) | Event::PI(_) => {}
            Event::Eof if stack.is_empty() && root_dict == Some(true) => {
                return Ok(identity_matches);
            }
            _ => return Err(DecodeError::Invalid),
        }
    }
}

fn valid_xml_char(ch: char) -> bool {
    matches!(ch, '\t' | '\n' | '\r' | '\u{20}'..='\u{d7ff}' | '\u{e000}'..='\u{fffd}' | '\u{10000}'..='\u{10ffff}')
}

fn valid_xml_scalar(kind: Scalar, text: &str) -> bool {
    match kind {
        Scalar::Key | Scalar::String | Scalar::Boolean => true,
        Scalar::Integer => {
            let text = text.trim();
            let (digits, radix) =
                if let Some(hex) = text.strip_prefix("0x").or_else(|| text.strip_prefix("0X")) {
                    (hex, 16)
                } else {
                    (text.strip_prefix(['+', '-']).unwrap_or(text), 10)
                };
            let mut last_digit = false;
            for ch in digits.chars() {
                if ch.is_digit(radix) {
                    last_digit = true;
                } else if ch == '_' && last_digit {
                    last_digit = false;
                } else {
                    return false;
                }
            }
            last_digit
        }
        Scalar::Real => text.trim().parse::<f64>().is_ok(),
        Scalar::Date => valid_xml_date(text.as_bytes()),
        Scalar::Data => {
            let compact: Vec<_> = text.bytes().filter(|b| !b.is_ascii_whitespace()).collect();
            base64::engine::general_purpose::STANDARD
                .decode(compact)
                .is_ok()
        }
    }
}

fn valid_xml_date(text: &[u8]) -> bool {
    fn date(text: &[u8]) -> Option<bool> {
        let [
            y0,
            y1,
            y2,
            y3,
            b'-',
            m0,
            m1,
            b'-',
            d0,
            d1,
            b'T',
            h0,
            h1,
            b':',
            n0,
            n1,
            b':',
            s0,
            s1,
            b'Z',
        ] = text
        else {
            return None;
        };
        let num = |bytes: &[u8]| {
            bytes.iter().try_fold(0u32, |value, &byte| {
                (byte.is_ascii_digit()).then(|| value * 10 + u32::from(byte - b'0'))
            })
        };
        let year = num(&[*y0, *y1, *y2, *y3])?;
        let month = num(&[*m0, *m1])?;
        let day = num(&[*d0, *d1])?;
        let last_day = match month {
            2 if year.is_multiple_of(4)
                && (!year.is_multiple_of(100) || year.is_multiple_of(400)) =>
            {
                29
            }
            2 => 28,
            4 | 6 | 9 | 11 => 30,
            1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
            _ => return None,
        };
        Some(
            year > 0
                && (1..=last_day).contains(&day)
                && num(&[*h0, *h1])? < 24
                && num(&[*n0, *n1])? < 60
                && num(&[*s0, *s1])? < 60,
        )
    }
    date(text) == Some(true)
}

struct Binary<'a> {
    bytes: &'a [u8],
    offsets: &'a [u8],
    offset_bytes: usize,
    ref_bytes: usize,
}

#[derive(Clone, Copy)]
enum Object<'a> {
    Other,
    Ascii(&'a [u8]),
    Utf16(&'a [u8]),
    Collection(Collection<'a>),
}

impl Object<'_> {
    fn matches(self, text: &str) -> bool {
        match self {
            Self::Ascii(bytes) => bytes == text.as_bytes(),
            Self::Utf16(bytes) => bytes
                .chunks_exact(2)
                .map(|b| u16::from_be_bytes([b[0], b[1]]))
                .eq(text.encode_utf16()),
            _ => false,
        }
    }

    fn validate<S: OriginalAuthorityOwner>(self, meter: &mut Meter<'_, '_, S>) -> Decode<()> {
        let good = match self {
            Self::Ascii(bytes) => {
                meter.add(bytes.len())?;
                bytes.is_ascii()
            }
            Self::Utf16(bytes) => {
                meter.add(bytes.len())?;
                char::decode_utf16(
                    bytes
                        .chunks_exact(2)
                        .map(|b| u16::from_be_bytes([b[0], b[1]])),
                )
                .all(|ch| ch.is_ok())
            }
            Self::Other | Self::Collection(_) => true,
        };
        invalid(good.then_some(()))
    }
}

#[derive(Clone, Copy)]
struct Collection<'a> {
    references: &'a [u8],
    dict_pairs: Option<usize>,
}

struct BinaryFrame<'a> {
    collection: Collection<'a>,
    next: usize,
    selected: bool,
}

/// Binary plists are object graphs. Follow each reachable object once; its
/// repeated references and cycles need no tree copies or recursive decoding.
fn binary_identity<S: OriginalAuthorityOwner>(
    raw: &[u8],
    expected: &str,
    meter: &mut Meter<'_, '_, S>,
) -> Decode<bool> {
    let end = invalid(raw.len().checked_sub(32))?;
    let trailer = &raw[end..];
    let offset_bytes = usize::from(trailer[6]);
    let ref_bytes = usize::from(trailer[7]);
    let count = invalid(binary_uint(&trailer[8..16]))?;
    let top = invalid(binary_uint(&trailer[16..24]))?;
    let table_start = invalid(binary_uint(&trailer[24..]))?;
    let table_len = invalid(count.checked_mul(offset_bytes))?;
    let table_end = invalid(table_start.checked_add(table_len))?;
    if offset_bytes == 0 || ref_bytes == 0 || top >= count || table_start < 8 || table_end > end {
        return Err(DecodeError::Invalid);
    }
    let table = Binary {
        bytes: &raw[..table_start],
        offsets: &raw[table_start..table_end],
        offset_bytes,
        ref_bytes,
    };
    let Object::Collection(root) = invalid(table.object(top))? else {
        return Err(DecodeError::Invalid);
    };
    if root.dict_pairs.is_none() {
        return Err(DecodeError::Invalid);
    }
    let capacity = invalid(count.checked_add(MAX_DEPTH * size_of::<BinaryFrame<'_>>()))?;
    let _capacity = meter.op.reserve(STAGE, capacity)?;
    meter.add(count)?;
    let mut visited = vec![false; count];
    visited[top] = true;
    let mut stack = Vec::with_capacity(MAX_DEPTH);
    stack.push(BinaryFrame {
        collection: root,
        next: 0,
        selected: false,
    });
    let mut identity_matches = false;
    loop {
        let is_root = stack.len() == 1;
        let Some(frame) = stack.last_mut() else {
            return Ok(identity_matches);
        };
        let slots = frame.collection.references.len() / ref_bytes;
        if frame.next == slots {
            stack.pop();
            continue;
        }
        meter.add(32 + ref_bytes + offset_bytes)?;
        let (slot, key) = if let Some(pairs) = frame.collection.dict_pairs {
            let key = frame.next.is_multiple_of(2);
            (frame.next / 2 + if key { 0 } else { pairs }, key)
        } else {
            (frame.next, false)
        };
        frame.next += 1;
        let start = slot * ref_bytes;
        let target = invalid(binary_uint(
            &frame.collection.references[start..start + ref_bytes],
        ))?;
        let seen = invalid(visited.get_mut(target))?;
        if *seen && !(is_root || key) {
            continue;
        }
        let object = invalid(table.object(target))?;
        if key && matches!(object, Object::Collection(_)) {
            return Err(DecodeError::Invalid);
        }
        if is_root {
            if key {
                frame.selected = object.matches(KEY);
            } else if frame.selected {
                meter.add(expected.len())?;
                identity_matches = object.matches(expected);
            }
        }
        if *seen {
            continue;
        }
        *seen = true;
        object.validate(meter)?;
        if let Object::Collection(collection) = object {
            if stack.len() == MAX_DEPTH {
                return Err(DecodeError::Invalid);
            }
            stack.push(BinaryFrame {
                collection,
                next: 0,
                selected: false,
            });
        }
    }
}

fn binary_uint(bytes: &[u8]) -> Option<usize> {
    bytes.iter().try_fold(0usize, |result, &byte| {
        result.checked_mul(256)?.checked_add(usize::from(byte))
    })
}

impl<'a> Binary<'a> {
    fn object(&self, reference: usize) -> Option<Object<'a>> {
        let start = reference.checked_mul(self.offset_bytes)?;
        let end = start.checked_add(self.offset_bytes)?;
        let offset = binary_uint(self.offsets.get(start..end)?)?;
        let (&token, bytes) = self.bytes.get(offset..)?.split_first()?;
        if offset < 8 {
            return None;
        }
        let size = usize::from(token & 15);
        let object = match token {
            0x00 | 0x08 | 0x09 | 0x0f => Object::Other,
            0x10..=0x1f => {
                bytes.get(..1 << size)?;
                Object::Other
            }
            0x22 => {
                bytes.get(..4)?;
                Object::Other
            }
            0x23 => {
                bytes.get(..8)?;
                Object::Other
            }
            0x33 => {
                let seconds = f64::from_be_bytes(bytes.get(..8)?.try_into().ok()?);
                if !(-63_113_904_000.0..252_423_993_600.0).contains(&seconds) {
                    return None;
                }
                Object::Other
            }
            0x80..=0x8f => {
                // CPython's UID constructor accepts values fitting in 64 bits.
                let uid = bytes.get(..size + 1)?;
                if uid[..uid.len().saturating_sub(8)].iter().any(|&b| b != 0) {
                    return None;
                }
                Object::Other
            }
            0x40..=0x6f | 0xa0..=0xaf | 0xd0..=0xdf => {
                let (length, data) = if size == 15 {
                    let (&size_token, tail) = bytes.split_first()?;
                    let length_bytes = 1 << (size_token & 3);
                    (
                        binary_uint(tail.get(..length_bytes)?)?,
                        tail.get(length_bytes..)?,
                    )
                } else {
                    (size, bytes)
                };
                match token >> 4 {
                    4 => {
                        data.get(..length)?;
                        Object::Other
                    }
                    5 => Object::Ascii(data.get(..length)?),
                    6 => Object::Utf16(data.get(..length.checked_mul(2)?)?),
                    10 | 13 => {
                        let dict = token >> 4 == 13;
                        let slots = length.checked_mul(if dict { 2 } else { 1 })?;
                        Object::Collection(Collection {
                            references: data.get(..slots.checked_mul(self.ref_bytes)?)?,
                            dict_pairs: dict.then_some(length),
                        })
                    }
                    _ => return None,
                }
            }
            _ => return None,
        };
        Some(object)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::original_authority::definitions::tests::{operation, with_source};
    use base64::engine::general_purpose::STANDARD;

    const EXPECTED: &str = "dev.tobkiri.雪";

    fn assert_rejection(error: OriginalAuthorityError, message: &str) {
        assert_eq!(error.class, RECOVERY_APP_DENIED);
        assert_eq!(
            error.message,
            format!("active Profile Shell artifact rejected: {message}")
        );
        assert!(matches!(error.cause, Some(OriginalAuthorityCause::Python {
            class: "tobkiri_protocol.errors.ProtocolError", message: inner, ..
        }) if inner == message));
    }

    #[tokio::test(flavor = "current_thread")]
    async fn bundle_metadata_xml_bom_duplicate_keys_and_string_type() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let xml = concat!(
                r#"<?xml version="1.0" encoding="UTF-16"?>"#,
                r#"<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">"#,
                "<plist><dict><key>CFBundleIdentifier</key><string>wrong</string>",
                "<key>CFBundleIdentifier</key><string><![CDATA[dev.tobkiri.]]>&#x96ea;</string>",
                "<key>CFBundleName</key><string>Example</string></dict></plist>"
            );
            let utf16 = [0xff, 0xfe]
                .into_iter()
                .chain(xml.encode_utf16().flat_map(u16::to_le_bytes))
                .collect::<Vec<_>>();
            check_bundle_identity(&op, budget, &utf16, EXPECTED).unwrap();

            let wrong = b"<plist><dict><key>CFBundleIdentifier</key><true/><key>nested</key><dict><key>CFBundleIdentifier</key><string>dev.tobkiri</string></dict></dict></plist>";
            assert_rejection(
                check_bundle_identity(&op, budget, wrong, "dev.tobkiri").unwrap_err(),
                MISMATCH,
            );

            for malformed in [
                b"<plist><dict><string>CFBundleIdentifier</string><string>dev.tobkiri</string></dict></plist>".as_slice(),
                b"<plist><dict><key>CFBundleIdentifier</key><key>dev.tobkiri</key></dict></plist>",
                b"<plist><dict><key>CFBundleIdentifier</key><string>dev.tobkiri&undefined;</string></dict></plist>",
                b"<?xml version='1.0'?><!DOCTYPE plist [<!ENTITY a 'dev.tobkiri'>]><plist><dict><key>CFBundleIdentifier</key><string>&a;</string></dict></plist>",
                b"<plist><dict><key>CFBundleIdentifier</key><string>dev.tobkiri</string></dict>",
            ] {
                assert_rejection(
                    check_bundle_identity(&op, budget, malformed, "dev.tobkiri").unwrap_err(),
                    INVALID,
                );
            }
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn bundle_metadata_binary_matches_python_plistlib_fixtures() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            // Generated by plistlib.dumps(..., fmt=FMT_BINARY, sort_keys=False).
            // The first contains a Unicode identity and an unrelated null in
            // an array. The optional integer/array key is a two-byte mutation
            // of a one-letter string key, checked against plistlib.loads.
            let unicode = STANDARD.decode("YnBsaXN0MDDTAQIDBAUGXxASQ0ZCdW5kbGVJZGVudGlmaWVyVG5hbWVWdGhpbmdzbQBkAGUAdgAuAHQAbwBiAGsAaQByAGkALpbqV1RvYmtpcmmjBwgJABAAQXgIDyQpMEtTV1haAAAAAAAAAQEAAAAAAAAACgAAAAAAAAAAAAAAAAAAAFw=").unwrap();
            check_bundle_identity(&op, budget, &unicode, EXPECTED).unwrap();

            let mut key = STANDARD.decode("YnBsaXN0MDDSAQIDBFFrXxASQ0ZCdW5kbGVJZGVudGlmaWVyV2lnbm9yZWRtAGQAZQB2AC4AdABvAGIAawBpAHIAaQAuluoIDQ8kLAAAAAAAAAEBAAAAAAAAAAUAAAAAAAAAAAAAAAAAAABH").unwrap();
            assert_eq!(&key[13..15], b"Qk");
            key[13..15].copy_from_slice(&[0x10, 1]);
            check_bundle_identity(&op, budget, &key, EXPECTED).unwrap();
            key[13..15].copy_from_slice(&[0xa1, 1]);
            assert_rejection(
                check_bundle_identity(&op, budget, &key, EXPECTED).unwrap_err(),
                INVALID,
            );

            let wrong_type = STANDARD.decode("YnBsaXN0MDDSAQIDBF8QEkNGQnVuZGxlSWRlbnRpZmllclZuZXN0ZWRLZGV2LnRvYmtpcmnRAQVbZGV2LnRvYmtpcmkIDSIpNTgAAAAAAAABAQAAAAAAAAAGAAAAAAAAAAAAAAAAAAAARA==").unwrap();
            assert_rejection(
                check_bundle_identity(&op, budget, &wrong_type, "dev.tobkiri").unwrap_err(),
                MISMATCH,
            );

            let mut invalid_offset = unicode.clone();
            *invalid_offset.last_mut().unwrap() = 255;
            assert_rejection(
                check_bundle_identity(&op, budget, &invalid_offset, EXPECTED).unwrap_err(),
                INVALID,
            );
        });
    }
}
