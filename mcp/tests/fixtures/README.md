# Fixture custody

These archives are assertion inputs. Normal test runs only read them. Refreshes
are deliberate, opt-in commands followed by a separate assertion run; a test
must never regenerate and validate the same bytes in one pass.

Baseline commit: `815a6616b227c3a6373180c0528d19a96296a62b`.

| Artifact | Immutable provenance / derivation | SHA-256 | Exact regeneration command |
|---|---|---|---|
| `../../../agent/src/snapshot_legacy.js` | Baseline `mcp/src/snapshot.js`, plus the structured `units` and `rendererIncomplete` mirror fields required by response shaping. The shared actor migration renames the internal DOM marker from `data-mcp-ref` to `data-rustwright-ref`; the legacy outline and traversal stay unchanged. The response-shape derivation is pinned in `snapshot_legacy-derivation.patch`. | `3e9be04b7cbbc6c3567c7341d09c168ed720a9fcd162006e1d5ac9236bc32340` | `git show 815a6616b227c3a6373180c0528d19a96296a62b:mcp/src/snapshot.js > mcp/src/snapshot_legacy.js && git apply mcp/tests/fixtures/snapshot_legacy-derivation.patch && python -c 'from pathlib import Path; p = Path("mcp/src/snapshot_legacy.js"); p.write_text(p.read_text().replace("data-mcp-ref", "data-rustwright-ref"))' && mv mcp/src/snapshot_legacy.js agent/src/snapshot_legacy.js` |
| `tools-list.json` | Complete canonical `mirror` catalog. | `addbebf233cadc7d8aed1cc6765920430eeda4623e01bccfb208b1386ce7ec75` | `RUSTWRIGHT_UPDATE_TOOL_CATALOG=1 cargo test --manifest-path mcp/Cargo.toml --locked catalog_matches_archived_bytes` |
| `union-legacy-surfaces.json` | Baseline decoded outputs used to prove the canonical profile changes the intended snapshot, console, and network surfaces. | `93022b7a449042e922b1dc3514edae7de0bf3e4ba844206dd50f984a2e1cd04d` | `LEGACY_FIXTURE_DIR="$(mktemp -d)"; git archive 815a6616b227c3a6373180c0528d19a96296a62b | tar -x -C "$LEGACY_FIXTURE_DIR"; node mcp/tests/fixtures/derive_union_legacy_surfaces.js "$LEGACY_FIXTURE_DIR"` |
| `rustwright-union-console.js` | Authored, content-addressed external-script fixture for the union corpus. Each console emission occupies its own stable source line. | `c16e1a852f660177de3071e65592840d0ac88a27355a5845e5caa1404f97af33` | Authored fixture; update deliberately and recalculate with `shasum -a 256 mcp/tests/fixtures/rustwright-union-console.js`. |

After a catalog refresh, run the same test command again without its
`RUSTWRIGHT_UPDATE_*` variable. After any recapture, verify the recorded digest
with:

```text
shasum -a 256 agent/src/snapshot_legacy.js \
  mcp/tests/fixtures/tools-list.json \
  mcp/tests/fixtures/union-legacy-surfaces.json \
  mcp/tests/fixtures/rustwright-union-console.js
```

The recapture commands above only write artifacts. Run assertions separately:

```text
cargo test --manifest-path mcp/Cargo.toml --locked catalog_matches_archived_bytes
cargo test --manifest-path mcp/Cargo.toml --locked canonical_profile_activates_every_reviewed_behavior_for_every_client
```

`snapshot_w2_test.js` is an authored deterministic harness, not a captured
golden. Browser-observable union fields are verified only in real-Chromium CI;
the normal test never updates its frozen expectations.
