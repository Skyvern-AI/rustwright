use std::{path::PathBuf, process::Command};

#[test]
fn deterministic_full_tree_distillation_and_find_contract() {
    let fixture =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/snapshot_w2_test.js");
    let output = Command::new("node")
        .arg(fixture)
        .output()
        .unwrap_or_else(|error| {
            panic!(
                "Node.js executable `node` is required for snapshot_w2; install Node.js and ensure `node` is on PATH: {error}"
            )
        });
    assert!(
        output.status.success(),
        "snapshot W2 fixture failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr),
    );
    assert_eq!(
        String::from_utf8_lossy(&output.stdout).trim(),
        "snapshot W2 deterministic fixtures: PASS"
    );
}
