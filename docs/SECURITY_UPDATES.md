# Security update 1.0.5

This source update addresses CodeQL alerts 1–23 and Dependabot alert 1.

- Route web redirects through one application-local URL validator, rejecting
  external and browser-normalized network paths while preserving local queries.
- Convert maintenance-window server selection to a validated positive integer
  before constructing the form action.
- Bound dataset names and validate components independently, avoiding expensive
  regular-expression backtracking on malformed replication destinations.
- Keep installed remote helpers and rollback copies root-only (0700), including
  the installer and in-place updates. The helper continues to run through sudo.
- Remove request-derived identifiers from refresh-failure log messages.
- Use explicit read-only workflow permissions and immutable Docker action pins.
- Make test environments use HTML autoescaping and assert exact TLS identities.
- Update Paramiko to 5.0.1 for CVE-2026-44405 and its RSA signing follow-up fix.
  SSH servers must support SHA-2 RSA signatures or modern keys such as Ed25519;
  obsolete SHA-1-only servers are no longer supported.
- Rename the frozen runtime requirements to `requirements-frozen.txt`, which
  Dependabot can discover; Docker and development installs use that same file.

The recommended remote helper version is 1.0.3. Update remote helpers to receive
the file-permission hardening. This source update does not publish a container
image or modify running NAS deployments.
