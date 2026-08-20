# Public Export Coverage

- source commit: `10b8f229bfbe2d82ea7bea749339c9c8a3ec9b4b`
- classification policy SHA-256: `bbec31abbe5292fd3340da0780ba342094037333087276b96fc7a67fa1fce111`
- raw source content included in this report: `false`
- unknown exclusions: `0` (generation fails closed on `unknown_blocked`)

## Accounting

| Measure | Count |
|---|---:|
| Tracked source files | 1061 |
| Public-classified source files | 728 |
| Materialized public files | 727 |
| Non-materialized control files | 1 |
| Excluded source files | 333 |
| Excluded files with executable behavior | 197 |
| Generated export receipt files | 1 |
| Generated export audit files | 2 |
| Export tracked files | 730 |

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
| `official_private` | 147 |
| `private_internal` | 186 |
| `public_generated` | 40 |
| `public_shared` | 688 |

## Exclusion reasons

| Reason code | Count |
|---|---:|
| `historical_research` | 13 |
| `internal_evidence_or_handoff` | 92 |
| `internal_test_boundary` | 58 |
| `live_identity_or_real_id` | 1 |
| `official_deployment_inventory` | 97 |
| `official_hosted_overlay` | 43 |
| `private_operation_runbook` | 29 |

The complete exclusion decision matrix is in `PUBLIC_EXCLUSION_MATRIX.json`.
Excluded source paths are represented only by SHA-256 digests; file bodies, credentials, names, private IDs, and live values are never copied.
