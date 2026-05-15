# ROADMAP

## Phase 1: Hardening & SDK Reference

- [x] **Manual Registration Reference:** Documents the three Python client
  registration patterns via `rucio.client.Client`: bare replica
  (`add_replicas`), atomic dataset creation with files (`add_dataset`),
  and appending to an existing dataset (`add_files_to_dataset`).
  See [user-workflows.md](./docs/user-workflows.md).
- [x] **Cloud-Native Dev Environment:** `.devcontainer` setup with a kind
  cluster pre-wired for local k8s development and Helm charts covering
  the full Rucio + FTS + Storage (XRootD, WebDAV, StoRM, MinIO) + OIDC
  (Keycloak) stack, mirroring the compose topology on Kubernetes.
- [x] **Bash → Pytest Migration:** All FTS-level and Rucio E2E tests
  migrated to self-contained pytest suites running inside service
  containers; shared infrastructure helpers extracted into `testbed.py`.
- [x] **Unified XRootD Helm Chart:** Single chart with `scitokens.enabled`
  flag replacing separate `xrootd` and `xrootd-scitokens` sub-charts.
- [x] **k8s GSI Fix:** Resolved five compounding issues preventing XRootD
  GSI TPC on Kubernetes (CA signing policies, file modes, static config,
  CRL level). Both runtimes now pass the full test suite.
- [x] **Teapot WebDAV multi-tenancy service:** Adds two Teapot instances
  (teapot1/teapot2) as WebDAV proxies with per-user StoRM-WebDAV instances
  authenticated via Keycloak OIDC tokens. Covers PROPFIND, PUT, GET, DELETE,
  unauthenticated rejection, FTS TPC between instances, and full Rucio
  conveyor → fts-oidc → Teapot TPC (TEAPOT1 → TEAPOT2). Requires openid
  scope in client-credentials grants for flaat /userinfo resolution.
- [x] **Rucio E2E transfers with S3 source:** Extend `test-rucio-transfers.py`
  with S3/MinIO RSE pairs analogous to `test-fts-with-s3.py`, covering the
  signed-URL path through the Rucio conveyor.
- [ ] **VO-based Teapot mapping with eduperson_entitlements:** Configure
  Keycloak to issue eduperson_entitlement claims alongside wlcg.groups and
  demonstrate Teapot's VO mapping mode as an alternative to FILE mapping.
  Requires group membership claims in the token, which the current
  service account path does not support.

## Phase 2: Failure Mode Validation

Three deterministic tests covering data integrity, auth lifecycle, and
infrastructure availability. All run with `--run-once` daemons; no
timing dependencies. Run with `make test-failure-modes`.

- [x] **Checksum Mismatch Injection** (`test-rucio-checksum-mismatch.py`)
  Registers a file with a deliberately wrong `adler32`. FTS detects the
  mismatch on dispatch and rejects the transfer; Rucio leaves the request FAILED.

- [x] **Source Replica Unavailability** (`test-rucio-replica-unavailable.py`)
  Two tests covering both layers of source unavailability:
  - **Operator-declared:** `availability_read=False` → submitter sets
    `NO_SOURCES` → rule STUCK in one cycle, no FTS dispatch.
  - **Unexpected outage:** `xrd1` container stopped → rule eventually
    reaches STUCK

- [x] **Token Expiry Mid-Transfer** (`test-rucio-token-expiry.py`)
  Patches Keycloak `accessTokenLifespan` to 30s, submits a StoRM OIDC
  transfer that outlasts the token, and asserts FINISHED — proving the
  conveyor and FTS OIDC refresh path handle expiry transparently.

## Out of Scope for Phase 2

The following failure modes were evaluated and intentionally excluded from
the deterministic (run-once) test suite:

- **Destination-side failure:** Higher complexity than source failure
  (partial writes, cleanup semantics require storage-level inspection).
  Source unavailability already validates the full error propagation path.
- **Auth/permission mismatch (wrong scope/audience):** High value for a
  dedicated OIDC hardening testbed but requires Keycloak client
  configuration changes beyond realm-level patches.
- **Conveyor/daemon failure:** The testbed runs daemons in `--run-once`
  mode; persistent daemon availability is outside the scope of integration
  testing. Covered in Phase 3.
- **Partial transfer/retry semantics:** Requires large files or `tc netem`
  network injection, which needs `NET_ADMIN` — incompatible with kind CI
  runners.

## Phase 3: Resilience & Chaos Validation (Optional)

This phase validates non-deterministic system behavior under realistic
operational conditions. Unlike Phase 2, tests here run with [long-running
live daemons (Helm deployment)](https://github.com/rucio/helm-charts/tree/master/charts/rucio-daemons), real polling/queue progression, and
time-based assertions against eventual consistency.

- [ ] **Bulk transfer under daemon load:** Validate throughput and stability
  under continuous submission pressure.
- [ ] **Daemon restart / recovery behavior:** Restart conveyor/FTS
  components mid-transfer and validate eventual recovery.
- [ ] **Queue backlog + catch-up behavior:** Force backlog accumulation and
  verify the system drains correctly.
- [ ] **Token refresh under long-lived daemon execution:** Validate sustained
  OIDC renewal across multiple polling cycles under a continuously running
  worker. Distinct from Phase 2's token expiry test, which validates the
  initial token fetch path in a single controlled cycle.

## Phase 4: Scale & Observability (Optional)

These are operational insights rather than correctness guarantees.

- [ ] **Bulk Transfer Benchmarks:** Submit N-file jobs via the FTS Python
  client and measure throughput. Informational only.
- [ ] **Prometheus / Grafana Integration:** FTS exposes metrics on port 8449;
  wire up a scrape config and a minimal dashboard for local observability
  during development.
- [ ] **Network Degradation Simulation:** `tc netem` on the Docker bridge to
  simulate WAN latency and packet loss. Requires `NET_ADMIN` capability —
  not compatible with kind CI runners without privilege escalation.
  Local-only option; not recommended for CI.
