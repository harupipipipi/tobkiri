//! Byte-level compatibility of coalesced SHA updates with the original v1
//! framing. Oracle allocations below exist only in these unit tests.

use super::*;

fn legacy_start(profile: &ProfileId) -> Vec<u8> {
    let mut bytes = b"tobkiri.audit.verified-prefix.v1\0".to_vec();
    bytes.extend_from_slice(&(profile.as_str().len() as u64).to_be_bytes());
    bytes.extend_from_slice(profile.as_str().as_bytes());
    bytes
}

fn legacy_row(
    bytes: &mut Vec<u8>,
    seq: i64,
    prev: &Digest,
    digest: &Digest,
    body: &[u8],
    event_id: Option<&Digest>,
) {
    // Independent oracle: append the original explicit field stream, without
    // the production header helper, offsets or fixed-size row buffers.
    bytes.extend_from_slice(b"row\0");
    let sequence = seq.to_be_bytes();
    for (index, sql_type, field) in [
        (0, 1, sequence.as_slice()),
        (1, 3, prev.as_str().as_bytes()),
        (2, 3, digest.as_str().as_bytes()),
        (3, 4, body),
        match event_id {
            Some(id) => (4, 3, id.as_str().as_bytes()),
            None => (4, 0, &[]),
        },
    ] {
        bytes.push(index);
        bytes.push(sql_type);
        bytes.extend_from_slice(&(field.len() as u64).to_be_bytes());
        bytes.extend_from_slice(field);
    }
}

fn assert_hash(proof: &VerifiedPrefix, bytes: &[u8]) {
    let actual: [u8; 32] = sha2::Digest::finalize(proof.state.clone()).into();
    let expected: [u8; 32] = <Sha256 as sha2::Digest>::digest(bytes).into();
    assert_eq!(actual, expected);
}

#[test]
fn coalesced_hash_matches_legacy_fields_for_null_ids_and_body_boundaries() {
    let profile = ProfileId::parse("prof_prefix_hash").unwrap();
    let prev = Digest::parse(&format!("sha256:{}", "0".repeat(64))).unwrap();
    let digest = Digest::parse(&format!("sha256:{}", "f".repeat(64))).unwrap();
    let event_id = Digest::hash(b"original event identity");
    for length in [
        0, 1, 7, 8, 9, 10, 31, 32, 55, 56, 63, 64, 65, 127, 128, 129, 65_535, 65_536,
    ] {
        let body: Vec<u8> = (0..length).map(|index| (index % 256) as u8).collect();
        for id in [None, Some(&event_id)] {
            let mut proof = VerifiedPrefix::empty(&profile);
            let mut legacy = legacy_start(&profile);
            proof.extend(1, &prev, &digest, &body, id).unwrap();
            legacy_row(&mut legacy, 1, &prev, &digest, &body, id);
            assert_hash(&proof, &legacy);
            assert_eq!(proof.rows, 1);
            assert_eq!(
                proof.tail.as_ref().unwrap().as_slice(),
                digest.as_str().as_bytes()
            );
        }
    }
}

#[test]
fn sequence_edge_encodings_and_overflow_keep_the_original_framing_and_checks() {
    let profile = ProfileId::parse("prof_prefix_sequence").unwrap();
    let prev = Digest::hash(b"previous row");
    let digest = Digest::hash(b"new row");
    let event_id = Digest::hash(b"event identity");
    for seq in [1, 2, 255, 256, i64::from(u32::MAX), i64::MAX] {
        for id in [None, Some(&event_id)] {
            let mut proof = VerifiedPrefix::empty(&profile);
            // Exercise the sequence field's edge encodings without allocating
            // billions of preceding rows. Both hash streams start identically.
            proof.rows = seq as u64 - 1;
            let mut legacy = legacy_start(&profile);
            proof.extend(seq, &prev, &digest, b"\0\xff", id).unwrap();
            legacy_row(&mut legacy, seq, &prev, &digest, b"\0\xff", id);
            assert_hash(&proof, &legacy);
            assert_eq!(proof.rows, seq as u64);
        }
    }

    for (rows, seq) in [(0, -1), (0, 0), (0, 2), (u64::MAX, 1)] {
        let mut proof = VerifiedPrefix::empty(&profile);
        proof.rows = rows;
        let before = sha2::Digest::finalize(proof.state.clone());
        assert!(matches!(
            proof.extend(seq, &prev, &digest, b"", None),
            Err(StoreError::Integrity(IntegrityFaultCode::AuditChain))
        ));
        assert_eq!(sha2::Digest::finalize(proof.state.clone()), before);
        assert_eq!(proof.rows, rows);
        assert!(proof.tail.is_none());
    }
}

#[test]
fn actual_sqlite_scan_and_expected_extensions_match_the_same_legacy_stream() {
    let profile = ProfileId::parse("prof_prefix_sqlite_hash").unwrap();
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(
        "CREATE TABLE audit(seq INTEGER,prev_digest TEXT,digest TEXT,body BLOB,event_id TEXT)",
    )
    .unwrap();
    let event_id = Digest::hash(b"identified row");
    let mut prev = Digest::hash(&[]);
    let mut expected = VerifiedPrefix::empty(&profile);
    let mut legacy = legacy_start(&profile);
    let maximum_body = vec![0xa5; 65_536];
    for (index, (body, id)) in [
        (b"".as_slice(), None),
        (b"\0\xffframed bytes".as_slice(), Some(&event_id)),
        (maximum_body.as_slice(), None),
    ]
    .into_iter()
    .enumerate()
    {
        let seq = index as i64 + 1;
        let digest = Digest::hash(body);
        conn.execute(
            "INSERT INTO audit(seq,prev_digest,digest,body,event_id) VALUES(?1,?2,?3,?4,?5)",
            rusqlite::params![
                seq,
                prev.as_str(),
                digest.as_str(),
                body,
                id.map(Digest::as_str)
            ],
        )
        .unwrap();
        expected.extend(seq, &prev, &digest, body, id).unwrap();
        legacy_row(&mut legacy, seq, &prev, &digest, body, id);
        let actual = VerifiedPrefix::scan(&conn, &profile).unwrap();
        assert!(expected.matches(&actual));
        assert_hash(&actual, &legacy);
        prev = digest;
    }
}
