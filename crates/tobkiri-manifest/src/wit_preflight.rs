//! Bound guest text BEFORE wit-parser's recursive AST construction.
use crate::ManifestError;

const MAX_DOCUMENT_BYTES: usize = 1024 * 1024;
const MAX_SYNTAX_DEPTH: usize = 64;
// Each token consumes at least one significant byte. Counting every such byte
// conservatively bounds tokens/AST breadth without allocating a token stream.
const MAX_SIGNIFICANT_BYTES: usize = 262_144;

fn invalid(message: &'static str) -> ManifestError {
    ManifestError::Invalid(message.into())
}

pub(crate) fn parse_guest_wit(
    path: &str,
    text: &str,
) -> Result<wit_parser::UnresolvedPackageGroup, ManifestError> {
    preflight(text)?;
    wit_parser::UnresolvedPackageGroup::parse(path, text)
        .map_err(|error| ManifestError::Invalid(error.to_string()))
}

fn preflight(text: &str) -> Result<(), ManifestError> {
    if text.len() > MAX_DOCUMENT_BYTES {
        return Err(invalid("bounded WIT syntax document exceeded"));
    }
    let bytes = text.as_bytes();
    let mut delimiters = [0u8; MAX_SYNTAX_DEPTH];
    let mut depth = 0usize;
    let mut significant = 0usize;
    let mut position = 0usize;
    while position < bytes.len() {
        match bytes[position] {
            b' ' | b'\t' | b'\r' | b'\n' => {
                position += 1;
                continue;
            }
            b'/' if bytes.get(position + 1) == Some(&b'/') => {
                position += 2;
                while position < bytes.len() && bytes[position] != b'\n' {
                    position += 1;
                }
                continue;
            }
            b'/' if bytes.get(position + 1) == Some(&b'*') => {
                position += 2;
                let mut comments = 1usize;
                while comments != 0 {
                    let Some(&byte) = bytes.get(position) else {
                        return Err(invalid("unterminated WIT syntax comment"));
                    };
                    if byte == b'/' && bytes.get(position + 1) == Some(&b'*') {
                        comments = comments
                            .checked_add(1)
                            .ok_or_else(|| invalid("bounded WIT comment nesting exceeded"))?;
                        position += 2;
                    } else if byte == b'*' && bytes.get(position + 1) == Some(&b'/') {
                        comments -= 1;
                        position += 2;
                    } else {
                        position += 1;
                    }
                }
                continue;
            }
            // The pinned WIT grammar has no quoted string/character literals.
            // Do not invent escape semantics that could hide delimiters from
            // this check but expose them to the authoritative tokenizer.
            b'"' | b'\'' | b'\\' | b'[' | b']' => {
                return Err(invalid("unsupported WIT syntax character"));
            }
            b'<' | b'{' | b'(' => {
                if depth == delimiters.len() {
                    return Err(invalid("bounded WIT syntax depth exceeded"));
                }
                delimiters[depth] = bytes[position];
                depth += 1;
            }
            b'>' | b'}' | b')' => {
                let expected = match bytes[position] {
                    b'>' => b'<',
                    b'}' => b'{',
                    _ => b'(',
                };
                if depth == 0 || delimiters[depth - 1] != expected {
                    return Err(invalid("unbalanced WIT syntax delimiter"));
                }
                depth -= 1;
            }
            b'-' if bytes.get(position + 1) == Some(&b'>') => {
                // `->` is one arrow, not a type's closing angle bracket.
                position += 1;
                significant += 1;
            }
            _ => {}
        }
        position += 1;
        significant += 1;
        if significant > MAX_SIGNIFICANT_BYTES {
            return Err(invalid("bounded WIT syntax work exceeded"));
        }
    }
    if depth != 0 {
        return Err(invalid("unbalanced WIT syntax delimiter"));
    }
    Ok(())
}
