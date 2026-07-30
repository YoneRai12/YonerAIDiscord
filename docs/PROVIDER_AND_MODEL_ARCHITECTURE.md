# Provider and model architecture — public-safe view

This public reference keeps one code-owned provider registry and one capability registry.
Provider credentials, private endpoints, operator incident history, and deployment-specific
overrides are not part of the public distribution.

- Active runtime routes remain deny-by-default and require explicit local configuration.
- Recommendation data is advisory only: candidates remain inactive, require probing, and do
  not claim readiness or live success.
- The current recommendation manifest is
  `provider-recommendations.rtx5090.v2.json`.
- Model/provider failures do not enable paid or remote fallback automatically.

See `PUBLIC_BOUNDARY.md`, `PUBLIC_CURRENT_STATUS.md`, and
`src/yonerai_discord/provider_registry/` for the public contract surface.
