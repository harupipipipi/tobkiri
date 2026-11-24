// Adapted from componentize-py build.rs, commit c0949b19d464f5d70bc1051195a3ae0e6a012df9.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT
// Function body preserved exactly; only visibility changed for this module.
use std::mem;
use wasm_encoder::{ComponentSectionId, Encode as _, RawSection, Section as _};
use wasmparser::{Parser, Payload};

pub fn strip(input: &[u8]) -> anyhow::Result<Vec<u8>> {
    // Adapted from https://github.com/bytecodealliance/wasm-tools/blob/main/src/bin/wasm-tools/strip.rs
    //
    // TODO: Move that code into e.g. `wasm_encoder` so we can reuse it here
    // instead of duplicating it.

    let mut output = Vec::new();
    let mut stack = Vec::new();

    for payload in Parser::new(0).parse_all(input) {
        let payload = payload?;

        // Track nesting depth, so that we don't mess with inner producer sections:
        match payload {
            Payload::Version { encoding, .. } => {
                output.extend_from_slice(match encoding {
                    wasmparser::Encoding::Component => &wasm_encoder::Component::HEADER,
                    wasmparser::Encoding::Module => &wasm_encoder::Module::HEADER,
                });
            }
            Payload::ModuleSection { .. } | Payload::ComponentSection { .. } => {
                stack.push(mem::take(&mut output));
                continue;
            }
            Payload::End { .. } => {
                let mut parent = match stack.pop() {
                    Some(c) => c,
                    None => break,
                };
                if output.starts_with(&wasm_encoder::Component::HEADER) {
                    parent.push(ComponentSectionId::Component as u8);
                    output.encode(&mut parent);
                } else {
                    parent.push(ComponentSectionId::CoreModule as u8);
                    output.encode(&mut parent);
                }
                output = parent;
            }
            _ => {}
        }

        if let Payload::CustomSection(ref c) = payload {
            let name = c.name();
            if name != "name" && !name.starts_with("component-type:") && name != "dylink.0" {
                continue;
            }
        }

        if let Some((id, range)) = payload.as_section() {
            RawSection {
                id,
                data: &input[range],
            }
            .append_to(&mut output);
        }
    }

    Ok(output)
}
