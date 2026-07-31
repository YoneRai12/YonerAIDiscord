# Public Export Coverage

- source commit: `a5a540fe5ab0ce1b9484c4d345a45f2be79db403`
- classification policy SHA-256: `e11c17da0055921622bad89991c626e7c8266e09f0a5f703ed637abc60bd8670`
- raw source content included in this report: `false`
- unknown exclusions: `0` (generation fails closed on `unknown_blocked`)

## Accounting

| Measure | Count |
|---|---:|
| Tracked source files | 945 |
| Public-classified source files | 706 |
| Materialized public files | 705 |
| Non-materialized control files | 1 |
| Excluded source files | 239 |
| Excluded files with executable behavior | 122 |
| Generated export receipt files | 1 |
| Generated export audit files | 2 |
| Export tracked files | 708 |

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
| `official_private` | 63 |
| `private_internal` | 176 |
| `public_generated` | 40 |
| `public_shared` | 666 |

## Exclusion reasons

| Reason code | Count |
|---|---:|
| `historical_research` | 13 |
| `internal_evidence_or_handoff` | 84 |
| `internal_test_boundary` | 56 |
| `live_identity_or_real_id` | 1 |
| `official_deployment_inventory` | 15 |
| `official_hosted_overlay` | 42 |
| `private_operation_runbook` | 28 |

The complete exclusion decision matrix is in `PUBLIC_EXCLUSION_MATRIX.json`.
Excluded source paths are represented only by SHA-256 digests; file bodies, credentials, names, private IDs, and live values are never copied.
