use super::*;
use crate::original_authority::definitions::tests::{operation, with_source};
use std::os::unix::fs::symlink;

const ORIGINAL_DIGEST: &str =
    "sha256:6790a1b36da71574839d093347174e1093f7ca8cccabbde14135cac74624f170";

#[tokio::test(flavor = "current_thread")]
async fn artifact_digest_matches_original_depth_first_unicode_fixture() {
    with_source(|source, read, cancellation, budget| {
        let op = operation(read, source, cancellation);
        let root = source.root.join("platform-artifacts");
        let selected = root.join("nested/Shell.app");
        fs::create_dir_all(selected.join("a")).unwrap();
        fs::create_dir_all(selected.join("empty")).unwrap();
        fs::write(selected.join("雪"), [0, 1, 255]).unwrap();
        fs::write(selected.join("beta"), b"beta contents").unwrap();
        fs::write(selected.join("a/é.txt"), "行\n").unwrap();
        fs::write(selected.join("a/one"), b"").unwrap();
        let parent = identity(&fs::metadata(&source.root).unwrap());
        let snapshot = capture(&op, budget, &root, "nested//./Shell.app", parent, None).unwrap();
        let names = snapshot
            .rows
            .values
            .iter()
            .scan(snapshot.path_end, |start, row| {
                let name = &snapshot.names.values[*start..row.name_end];
                *start = row.name_end;
                Some((row.role, std::str::from_utf8(name).unwrap()))
            })
            .collect::<Vec<_>>();
        assert_eq!(
            names,
            [
                (Role::Root, ""),
                (Role::Prefix, "nested"),
                (Role::Tree(0), "."),
                (Role::Tree(1), "a"),
                (Role::Tree(2), "one"),
                (Role::Tree(2), "é.txt"),
                (Role::Tree(1), "beta"),
                (Role::Tree(1), "empty"),
                (Role::Tree(1), "雪"),
            ],
        );
        verify_tree_digest(
            &op,
            budget,
            &root,
            "nested/Shell.app",
            parent,
            &snapshot,
            ORIGINAL_DIGEST,
        )
        .unwrap();
        let second = capture(&op, budget, &root, "nested/Shell.app", parent, None).unwrap();
        require_same(&op, budget, &snapshot, &second).unwrap();
        let error = verify_tree_digest(
            &op,
            budget,
            &root,
            "nested/Shell.app",
            parent,
            &snapshot,
            "sha256:0000000000000000000000000000000000000000000000000000000000000000",
        )
        .unwrap_err();
        assert!(error.message.contains("sentinel digest"));
    });
}

#[tokio::test(flavor = "current_thread")]
async fn artifact_identity_fences_full_mode_link_count_and_renamed_members() {
    with_source(|source, read, cancellation, budget| {
        let op = operation(read, source, cancellation);
        let root = source.root.join("platform-artifacts");
        let selected = root.join("Shell.app");
        fs::create_dir_all(&selected).unwrap();
        let file = selected.join("program");
        fs::write(&file, b"first").unwrap();
        let parent = identity(&fs::metadata(&source.root).unwrap());
        let capture_current = || capture(&op, budget, &root, "Shell.app", parent, None).unwrap();
        let expected = capture_current();

        let mode = fs::metadata(&file).unwrap().permissions().mode();
        fs::set_permissions(&file, fs::Permissions::from_mode(mode ^ 0o100)).unwrap();
        let changed = capture_current();
        let error = require_same(&op, budget, &expected, &changed).unwrap_err();
        assert_eq!(error.message, CHANGED);
        let file_row =
            |snapshot: &ArtifactTreeIdentity| snapshot.rows.values.last().unwrap().metadata;
        assert_eq!(file_row(&changed).mode, file_row(&expected).mode ^ 0o100);

        let outside = source.root.join("hardlink");
        fs::hard_link(&file, &outside).unwrap();
        let linked = capture_current();
        assert_eq!(file_row(&linked).links, file_row(&changed).links + 1);
        assert_eq!(
            require_same(&op, budget, &changed, &linked)
                .unwrap_err()
                .message,
            CHANGED
        );

        fs::rename(&file, selected.join("renamed")).unwrap();
        let moved = capture_current();
        assert_eq!(
            require_same(&op, budget, &linked, &moved)
                .unwrap_err()
                .message,
            CHANGED
        );
        assert_eq!(file_row(&moved).inode, file_row(&linked).inode);
    });
}

#[tokio::test(flavor = "current_thread")]
async fn artifact_identity_refuses_symlinks_and_preserves_missing_stat_cause() {
    with_source(|source, read, cancellation, budget| {
        let op = operation(read, source, cancellation);
        fs::create_dir_all(&source.root).unwrap();
        let root = source.root.join("platform-artifacts");
        let parent = identity(&fs::metadata(&source.root).unwrap());
        let error = |relative: &str| match capture(&op, budget, &root, relative, parent, None) {
            Ok(_) => panic!("unexpected successful artifact identity capture"),
            Err(error) => {
                assert_eq!(error.class, RECOVERY_APP_DENIED);
                error
            }
        };
        symlink("missing", &root).unwrap();
        assert_eq!(error("nested/Shell").message, SYMLINK);
        fs::remove_file(&root).unwrap();
        fs::create_dir(&root).unwrap();
        symlink("missing", root.join("nested")).unwrap();
        assert_eq!(error("nested/Shell").message, SYMLINK);
        fs::remove_file(root.join("nested")).unwrap();
        fs::create_dir_all(root.join("nested/Shell")).unwrap();
        symlink("missing", root.join("nested/Shell/leaf")).unwrap();
        assert_eq!(error("nested/Shell").message, SYMLINK);
        let error = error("not present");
        assert_eq!(error.message, UNAVAILABLE);
        assert!(matches!(
            error.cause,
            Some(OriginalAuthorityCause::Io(io)) if io.errno == Some(libc::ENOENT)
        ));
    });
}
