use super::*;
use crate::original_authority::definitions::tests::{lock_free, operation, with_setup_source};

const ACTIVATION_ID: &str = "snapshot\0exact";

#[tokio::test(flavor = "current_thread")]
async fn original_active_reservation_snapshot_keeps_only_five_fields_and_their_storage_types() {
    with_setup_source(|source, read, cancellation, _| {
        let op = operation(read, source, cancellation);
        with_registered_store(&op, &source.root, |store| {
            // A historical table shape may omit a projected column entirely.
            let path = source.root.join("authority/v4.sqlite3");
            Connection::open(&path)
                .unwrap()
                .execute_batch(
                    "DROP TABLE activation_reservations;
                     CREATE TABLE activation_reservations (
                         state, security_epoch, plan_digest, fencing_token,
                         activation_id, audit_note, profile_id
                     );
                     INSERT INTO activation_reservations VALUES
                         ('active', 4.0, NULL, 8, CAST(x'736e617073686f74006578616374' AS TEXT),
                          'unprojected', x'006f'),
                         ('retired', 2, 'wrong-state', 3, CAST(x'736e617073686f74006578616374' AS TEXT),
                          CAST(x'ff' AS TEXT), 'ignored'),
                         ('active', 3, 'wrong-id', 1, 'snapshot', CAST(x'ff' AS TEXT), 'ignored');",
                )
                .unwrap();

            let row = read_snapshot_binding(&op, store, ACTIVATION_ID)?.expect("only exact match");
            assert_eq!(
                row.value,
                [
                    Some(Value::Blob(vec![0, b'o'])),
                    Some(Value::Null),
                    None,
                    Some(Value::Real(4.0)),
                    Some(Value::Integer(8)),
                ]
            );
            assert!(read_snapshot_binding(&op, store, "SNAPSHOT\0exact")?.is_none());

            Connection::open(&path)
                .unwrap()
                .execute_batch(
                    "ALTER TABLE activation_reservations ADD COLUMN profile_authority_digest;
                     UPDATE activation_reservations SET profile_authority_digest='authority';",
                )
                .unwrap();
            let row = read_snapshot_binding(&op, store, ACTIVATION_ID)?.expect("same exact match");
            assert_eq!(row[2], Some(Value::Text("authority".into())));
            Ok(())
        })
        .unwrap();
        assert!(lock_free(
            &source.root.join("authority/.v4.sqlite3.lifecycle.lock")
        ));
        assert_eq!(read.receipt().open_connections, 0);
    });
}

#[tokio::test(flavor = "current_thread")]
async fn original_active_reservation_snapshot_fetches_past_duplicates_before_classifying() {
    with_setup_source(|source, read, cancellation, _| {
        let op = operation(read, source, cancellation);
        with_registered_store(&op, &source.root, |store| {
            let path = source.root.join("authority/v4.sqlite3");
            let db = Connection::open(&path).unwrap();
            db.execute_batch(
                "DROP TABLE activation_reservations;
                 CREATE TABLE activation_reservations (activation_id, state, profile_id, unused);
                 INSERT INTO activation_reservations VALUES
                     ('duplicate', 'active', 'first', 'valid'),
                     ('duplicate', 'active', 'second', 'valid');",
            )
            .unwrap();
            db.close().unwrap();

            let duplicate = read_snapshot_binding(&op, store, "duplicate")
                .err()
                .expect("two active matches");
            assert_eq!(duplicate.stage, STAGE);
            assert_eq!(duplicate.message, "multiple active authority reservations exist");
            assert!(duplicate.cause.is_none());

            let db = Connection::open(&path).unwrap();
            db.execute_batch(
                "INSERT INTO activation_reservations VALUES
                     ('duplicate', 'active', 'third', CAST(x'61ff0062' AS TEXT));",
            )
            .unwrap();
            db.close().unwrap();
            let invalid_text = read_snapshot_binding(&op, store, "duplicate")
                .err()
                .expect("fetchall decodes even an unused value in the third row");
            assert_eq!(invalid_text.stage, STAGE);
            assert_eq!(invalid_text.message, "active activation read failed");
            assert!(matches!(
                invalid_text.cause,
                Some(OriginalAuthorityCause::Python { class: "sqlite3.OperationalError", message, .. })
                    if message == "Could not decode to UTF-8 column 'unused' with text 'a�'"
            ));

            let db = Connection::open(&path).unwrap();
            db.execute_batch(
                "ALTER TABLE activation_reservations RENAME TO samples;
                 CREATE VIEW activation_reservations AS
                     SELECT activation_id, state, profile_id,
                            CASE WHEN profile_id='third' THEN abs(-9223372036854775808)
                                 ELSE 'valid' END AS unused
                     FROM samples;",
            )
            .unwrap();
            db.close().unwrap();
            let step = read_snapshot_binding(&op, store, "duplicate")
                .err()
                .expect("fetchall advances into the third row");
            assert_eq!(step.stage, STAGE);
            assert_eq!(step.message, "active activation read failed");
            assert!(matches!(
                step.cause,
                Some(OriginalAuthorityCause::Python { class: "sqlite3.OperationalError", message, .. })
                    if message == "integer overflow"
            ));
            Ok(())
        })
        .unwrap();
        assert_eq!(read.receipt().open_connections, 0);
    });
}
