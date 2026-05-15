# Known Issues and Operational Notes

This document tracks known bugs, open investigations, workarounds, and
intentional design decisions with security or operational implications in the
rucio-storage-testbed. The goal is to prevent re-investigation of issues that
have already been examined.

## Security Posture

### rucio-client and fts pods have cluster exec permissions on k8s

The `rucio-client`, `fts`, and `fts-oidc` pods ship with `kubectl` and a
ServiceAccount that grants `get`/`list`/`create` on `pods/exec`, `pods`, and
`deployments` within the testbed namespace. This lets the pytest suites
(`test-rucio-transfers.py`, `test-fts-with-storm-webdav.py`) orchestrate
seed/setup operations against storage pods the same way the compose version
uses the Docker socket.

This is appropriate for a development testbed but should be reviewed before
adopting the chart in shared or production-adjacent clusters. All
ServiceAccounts are namespace-scoped (Role/RoleBinding, not ClusterRole), so
blast radius is limited to the testbed namespace.

### Docker socket mounted into rucio-client, fts and fts-oidc containers (compose)

The compose stack mounts `/var/run/docker.sock` into the `rucio-client`, `fts` and `fts-oidc`
containers so that `svc_exec` helpers in the test suite can reach sibling
containers. This grants root-equivalent access to the Docker daemon on the
host. Acceptable for a local development testbed; remove the bind mount before
running in any shared environment.
