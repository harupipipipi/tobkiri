use std::sync::OnceLock;

struct Tables {
    nonprintable: Vec<(u32, u32)>,
    whitespace: Vec<u32>,
    decimal_zeroes: Vec<u32>,
}

fn tables() -> &'static Tables {
    static TABLES: OnceLock<Tables> = OnceLock::new();
    TABLES.get_or_init(|| {
        // This sidecar is produced by the reference CPython 3.13 / Unicode
        // 15.1 oracle, retaining its actual runtime and source identities.
        let table: serde_json::Value = serde_json::from_str(include_str!("unicode15_1.json"))
            .expect("checked-in CPython Unicode table is valid JSON");
        assert_eq!(table["schema"], "io.tobkiri.python-unicode-compat.v1");
        assert_eq!(table["unicode_version"], "15.1.0");
        let numbers = |field: &str| {
            table[field]
                .as_array()
                .expect("Unicode scalar list")
                .iter()
                .map(|value| value.as_u64().expect("Unicode scalar") as u32)
                .collect()
        };
        Tables {
            nonprintable: table["isprintable_false_ranges_inclusive"]
                .as_array()
                .expect("Unicode range list")
                .iter()
                .map(|range| {
                    let range = range.as_array().expect("inclusive Unicode range");
                    (
                        range[0].as_u64().expect("range start") as u32,
                        range[1].as_u64().expect("range end") as u32,
                    )
                })
                .collect(),
            whitespace: numbers("isspace_codepoints"),
            decimal_zeroes: numbers("decimal_zero_codepoints"),
        }
    })
}

pub(crate) fn is_printable(character: char) -> bool {
    let scalar = character as u32;
    let ranges = &tables().nonprintable;
    let insertion = ranges.partition_point(|(start, _)| *start <= scalar);
    insertion == 0 || scalar > ranges[insertion - 1].1
}

pub(crate) fn is_space(character: char) -> bool {
    tables()
        .whitespace
        .binary_search(&(character as u32))
        .is_ok()
}

pub(crate) fn decimal(character: char) -> Option<u8> {
    let scalar = character as u32;
    let zeroes = &tables().decimal_zeroes;
    let insertion = zeroes.partition_point(|zero| *zero <= scalar);
    if insertion == 0 {
        return None;
    }
    let value = scalar - zeroes[insertion - 1];
    (value < 10).then_some(value as u8)
}
