# Public Export Coverage

- source commit: `a9423f407de8769bed4d71e3d06524515ea936d7`
- classification policy SHA-256: `6b36973c02550176dcba3965c4fe975632cee14facd3c49d9b1255ace59af590`
- raw source content included in this report: `false`
- unknown exclusions: `0` (generation fails closed on `unknown_blocked`)

## Accounting

| Measure | Count |
|---|---:|
| Tracked source files | 956 |
| Public-classified source files | 707 |
| Materialized public files | 706 |
| Non-materialized control files | 1 |
| Excluded source files | 249 |
| Excluded files with executable behavior | 128 |
| Generated export receipt files | 1 |
| Generated export audit files | 2 |
| Export tracked files | 709 |

The control manifest is classified as a public source control but is not copied into the export.
`PUBLIC_EXPORT_MANIFEST.json` is generated after materialization and has no tracked source row.

```text
public_classified_source_count = materialized_file_count + non_materialized_control_file_count
tracked_file_count = public_classified_source_count + excluded_source_file_count
export_tracked_file_count = materialized_file_count + generated_audit_file_count + generated_receipt_file_count
```

## Source classifications

| Classification | Count |
|---|---:|
| `official_private` | 71 |
| `private_internal` | 178 |
| `public_generated` | 40 |
| `public_shared` | 667 |

## Exclusion reasons

| Reason code | Count |
|---|---:|
| `historical_research` | 13 |
| `internal_evidence_or_handoff` | 85 |
| `internal_test_boundary` | 57 |
| `live_identity_or_real_id` | 1 |
| `official_deployment_inventory` | 23 |
| `official_hosted_overlay` | 42 |
| `private_operation_runbook` | 28 |

The complete exclusion decision matrix is in `PUBLIC_EXCLUSION_MATRIX.json`.
Excluded source paths are represented only by SHA-256 digests; file bodies, credentials, names, private IDs, and live values are never copied.
