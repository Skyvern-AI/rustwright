# Fixture custody

These archives are assertion inputs. Normal test runs only read them. Refreshes
are deliberate, opt-in commands followed by a separate assertion run; a test
must never regenerate and validate the same bytes in one pass.

Baseline commit: `815a6616b227c3a6373180c0528d19a96296a62b`.

| Artifact | Immutable provenance / derivation | SHA-256 | Exact regeneration command |
|---|---|---|---|
| `../../../agent/src/snapshot_legacy.js` | Baseline `mcp/src/snapshot.js`, plus the structured `units` and `rendererIncomplete` mirror fields required by response shaping. The shared actor migration renames the internal DOM marker from `data-mcp-ref` to `data-rustwright-ref`; the legacy outline and traversal stay unchanged. The response-shape derivation is pinned in `snapshot_legacy-derivation.patch`. | `3e9be04b7cbbc6c3567c7341d09c168ed720a9fcd162006e1d5ac9236bc32340` | `git show 815a6616b227c3a6373180c0528d19a96296a62b:mcp/src/snapshot.js > mcp/src/snapshot_legacy.js && git apply mcp/tests/fixtures/snapshot_legacy-derivation.patch && python -c 'from pathlib import Path; p = Path("mcp/src/snapshot_legacy.js"); p.write_text(p.read_text().replace("data-mcp-ref", "data-rustwright-ref"))' && mv mcp/src/snapshot_legacy.js agent/src/snapshot_legacy.js` |
| `tools-list-legacy.json` | Complete `mirror` catalog serialized through the retained legacy-description path. The descriptions and schemas originate at the baseline commit above. | `6083c18fb648153975998bbde2172523fa6fdd1aa74637d5990727ca19239f56` | `RUSTWRIGHT_UPDATE_LEGACY_TOOL_CATALOG=1 cargo test --manifest-path mcp/Cargo.toml --locked legacy_catalog_matches_archived_pre_w5_bytes` |
| `tools-list-lean.json` | Complete `mirror` catalog serialized through the W5 lean-description path. | `addbebf233cadc7d8aed1cc6765920430eeda4623e01bccfb208b1386ce7ec75` | `RUSTWRIGHT_UPDATE_LEAN_TOOL_CATALOG=1 cargo test --manifest-path mcp/Cargo.toml --locked lean_catalog_matches_archived_w5_bytes` |
| `union-legacy-surfaces.json` | Exact decoded all-off union outputs. Navigation and snapshot were derived offline by running the union DOM through `mcp/src/snapshot.js` extracted from the baseline archive. Console and network are browser-observable: their exact renderings are pinned from the hash-checked baseline `actor.rs` formatter and the certified legacy-path W4 tests. The console template preserves the injected Proxy's raw anonymous top-frame line; `{PAGE_URL}` is the sole network-template input. | `93022b7a449042e922b1dc3514edae7de0bf3e4ba844206dd50f984a2e1cd04d` | `LEGACY_FIXTURE_DIR="$(mktemp -d)"; git archive 815a6616b227c3a6373180c0528d19a96296a62b | tar -x -C "$LEGACY_FIXTURE_DIR"; node mcp/tests/fixtures/derive_union_legacy_surfaces.js "$LEGACY_FIXTURE_DIR" > mcp/tests/fixtures/union-legacy-surfaces.json` |
| `rustwright-union-console.js` | Authored, content-addressed external-script fixture for the union corpus. Each console emission occupies its own stable source line; Chromium/CDP reports the warning calls on zero-based lines 1 and 2. | `c16e1a852f660177de3071e65592840d0ac88a27355a5845e5caa1404f97af33` | Authored fixture; update deliberately and recalculate with `shasum -a 256 mcp/tests/fixtures/rustwright-union-console.js`. |
| `codex-mcp-initialize-0.146.0.json` | Captured first MCP initialize frame from `@openai/codex@0.146.0`; the fixture records the capture date plus SHA-256 digests of that package's JavaScript launcher and platform-native binary. | `1dbd670a45fbac8a923b522e90c673d0c95c5b920a14ac6f6163bb39331ebd81` | `CAPTURE_DIR="$(mktemp -d)"; npm install --prefix "$CAPTURE_DIR" @openai/codex@0.146.0; cargo build --manifest-path mcp/Cargo.toml --locked; CODEX_HOME="$CAPTURE_DIR/home" node "$CAPTURE_DIR/node_modules/@openai/codex/bin/codex.js" -c "mcp_servers.capture.command='sh'" -c "mcp_servers.capture.args=['-c','tee $CAPTURE_DIR/frames.jsonl | mcp/target/debug/rustwright-mcp']" exec 'Return the word ready.'; CODEX_LAUNCHER="$CAPTURE_DIR/node_modules/@openai/codex/bin/codex.js"; CODEX_NATIVE="$(find "$CAPTURE_DIR/node_modules" -path '*/bin/codex' -type f | head -n 1)"; jq -n --arg captured "$(date -u +%F)" --arg launcher "$(shasum -a 256 "$CODEX_LAUNCHER" | awk '{print $1}')" --arg native "$(shasum -a 256 "$CODEX_NATIVE" | awk '{print $1}')" --slurpfile initialize <(head -n 1 "$CAPTURE_DIR/frames.jsonl") '{captured_at_utc:$captured,codex_cli_version:"0.146.0",codex_js_launcher_sha256:$launcher,codex_native_binary_sha256:$native,initialize_frame:$initialize[0],schema_version:1}' > mcp/tests/fixtures/codex-mcp-initialize-0.146.0.json` |

After a catalog refresh, run the same test command again without its
`RUSTWRIGHT_UPDATE_*` variable. After any recapture, verify the recorded digest
with:

```text
shasum -a 256 agent/src/snapshot_legacy.js \
  mcp/tests/fixtures/codex-mcp-initialize-0.146.0.json \
  mcp/tests/fixtures/tools-list-legacy.json \
  mcp/tests/fixtures/tools-list-lean.json \
  mcp/tests/fixtures/union-legacy-surfaces.json \
  mcp/tests/fixtures/rustwright-union-console.js
```

The recapture commands above only write artifacts. Run assertions separately:

```text
cargo test --manifest-path mcp/Cargo.toml --locked captured_codex_initialize_client_selects_budget_profile
cargo test --manifest-path mcp/Cargo.toml --locked canonical_all_off_transcript_matches_pre_treatment_union_expectations
```

`snapshot_w2_test.js` is an authored deterministic harness, not a captured
golden. Browser-observable union fields are verified only in real-Chromium CI;
the normal test never updates its frozen expectations.
