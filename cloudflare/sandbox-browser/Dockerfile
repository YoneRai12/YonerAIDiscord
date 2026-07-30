# Template only: owner must set this to the exact version paired with @cloudflare/sandbox.
# This Stage does not install or launch Chromium/Playwright.
ARG SANDBOX_IMAGE
FROM ${SANDBOX_IMAGE}

# Preserve the Cloudflare Sandbox base entrypoint.  No host mounts, secrets,
# browser cache, package install, or command entrypoint is configured here.
