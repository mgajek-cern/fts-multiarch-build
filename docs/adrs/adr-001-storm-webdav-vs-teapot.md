---
status: accepted
date: 2026-05-10
decision-makers: testbed maintainers
consulted: /
informed: /
---

# Keep direct StoRM WebDAV as the storage backend; defer Teapot adoption

## Context and Problem Statement

The testbed demonstrates an end-to-end OIDC-authenticated transfer pipeline: Rucio conveyor → FTS-OIDC → StoRM WebDAV TPC, using Keycloak-issued bearer tokens. The current implementation uses two statically-configured StoRM WebDAV v1.12.0 instances (`storm1`, `storm2`) sharing a single POSIX user and a single storage area.

Teapot (developed in the interTwin project) is a Python/FastAPI orchestrator that sits in front of StoRM WebDAV and spawns per-user StoRM WebDAV JVM instances on demand, providing filesystem-level uid isolation and multi-tenancy. The question is whether to replace the current static StoRM deployment with a Teapot-based one, or keep the current architecture and document the migration path for when multi-tenancy becomes required.

## Decision Drivers

* Current testbed must continue to demonstrate the full OIDC transfer chain (Rucio → FTS → StoRM) without regression
* Complexity added to the testbed should be justified by a concrete, near-term use case
* Dev-vs-prod migration path must be documented so downstream adopters know what to change
* Debugging and onboarding burden for new contributors should stay low
* Multiple OIDC issuers should be supportable (e.g., local Keycloak alongside a federated IdP)
* Per-user POSIX uid isolation is not currently required by any planned testbed use case

## Considered Options

* Keep direct StoRM WebDAV (status quo), document the Teapot migration path
* Replace StoRM WebDAV with Teapot now
* Run both side-by-side in the testbed

## Decision Outcome

Chosen option: **Keep direct StoRM WebDAV and document the Teapot migration path**, because the current architecture is sufficient to demonstrate OIDC-driven data orchestration, Teapot solves a problem (per-user uid isolation) that no current testbed use case requires, and introducing Teapot now would force re-validation of a carefully-tuned OIDC chain without delivering matching value.

### Consequences

* Good, because the working Rucio → FTS → StoRM OIDC transfer path is preserved and remains easy to reason about
* Good, because configuration surface stays small (StoRM environment variables + two Spring profile YAMLs + one storage-area properties file)
* Good, because multiple OIDC issuers can be added to StoRM directly via `oauth.issuers` without deploying additional orchestrators
* Good, because dev-vs-prod differences are captured in a documented migration path rather than in runtime complexity
* Bad, because the testbed does not demonstrate per-user uid isolation, which could be relevant for future multi-tenant deployments
* Bad, because adopting Teapot later requires a coordinated migration (estimated ~9 working days, see "More Information")
* Neutral, because the testbed's cert-trust workarounds (self-signed CA bootstrap, davix/Neon system-trust updates, scheme compatibility patches) apply regardless of whether Teapot is used — they are driven by self-signed certs, not by the StoRM/Teapot choice
* Update (2026-05-10): The "run both side-by-side" option was implemented as an additive experiment branch. storm1/storm2 direct TPC remains the canonical OIDC transfer path; Teapot is added as a parallel service demonstrating multi-tenancy. The ADR migration estimate of ~9 working days proved accurate. The single trusted_OP limitation and per-request
JVM spawn cost (~30s cold start) were confirmed as real constraints. TPC via Teapot was not implemented — single-instance user mapping makes it a loopback transfer; two Teapot deployments would be required for a meaningful cross-node test.

### Confirmation

Compliance with this decision is confirmed by:

* `./shared/tests/test-fts-with-storm-webdav.py` passes (direct FTS-OIDC → StoRM TPC)
* `./shared/tests/test-rucio-transfers.py` with `run_storm_oidc_transfer_test` passes (Rucio conveyor → FTS-OIDC → StoRM TPC)
* `deploy/compose/docker-compose.yml` contains only `storm1` and `storm2`, no `teapot` service
* `t_file.src_token_id` and `t_file.dst_token_id` are populated for conveyor-submitted transfers (proves OIDC tokens are attached per file)
* Rule state transitions to `OK` with `Locks OK/REPLICATING/STUCK: 1/0/0` for STORM1 → STORM2 replication rules

If any of these fail, the decision should be revisited rather than worked around.

## Pros and Cons of the Options

### Keep direct StoRM WebDAV (status quo)

Current architecture: two static StoRM WebDAV JVMs, shared POSIX user, Keycloak OIDC, Spring profile configs, WLCG scope-based authorization.

* Good, because the OIDC transfer path has been debugged end-to-end and is reproducible
* Good, because StoRM WebDAV supports multiple OIDC issuers natively via `oauth.issuers` list
* Good, because the official `ghcr.io/italiangrid/storm-webdav:v1.12.0` image is used — no custom packaging
* Good, because startup is deterministic (ports 8443/8085 always, no per-request JVM spawn latency)
* Good, because StoRM WebDAV is deployed in production at multiple Tier-1/Tier-2 sites
* Neutral, because the TPC pair uses `http://` source (port 8085) + `davs://` destination (port 8443), which requires patching Rucio's scheme compatibility map — this is a self-signed-cert workaround, not a StoRM limitation
* Bad, because there is no per-user POSIX uid isolation — all files owned by a single `storm` user
* Bad, because demonstrating multi-tenancy would require running additional StoRM containers per tenant, which does not scale

### Replace StoRM WebDAV with Teapot now

Teapot-based architecture: single Teapot orchestrator, per-user StoRM WebDAV JVMs spawned on demand, VO group → POSIX username mapping, dynamic port allocation.

* Good, because per-user JVMs provide filesystem-level uid isolation
* Good, because idle JVMs are torn down automatically after `INSTANCE_TIMEOUT_SEC`, reducing long-running memory footprint for sparse multi-tenant workloads
* Good, because it matches the interTwin reference pattern for multi-tenant StoRM deployments
* Neutral, because token validation, scope-based authz, and TPC support are delegated to StoRM — so the auth semantics are identical
* Bad, because Teapot supports only a single `trusted_OP` per instance; supporting multiple IdPs requires multiple Teapot deployments
* Bad, because dynamic port allocation (32400+) complicates Rucio RSE URL construction — either use Teapot as a reverse proxy (Option A, simpler but adds hops) or implement a custom `lfn2pfn` policy package (Option B, substantial effort)
* Bad, because the first request from a new user pays a ~60s JVM spawn cost, which is incompatible with CI timeouts without careful tuning
* Bad, because debugging token forwarding through an additional orchestration layer on top of an already TLS-sensitive stack increases onboarding burden
* Bad, because re-validating the Rucio conveyor integration, FTS audience resolution, and TPC flow would invalidate the current testbed's proven-working state

### Run both side-by-side

Keep `storm1` + `storm2` and add a `teapot` service alongside, registering a separate `TEAPOT1` RSE.

* Good, because it preserves the existing working tests while adding the multi-tenancy demonstration
* Good, because it supports direct comparison of the two deployment patterns in the same environment
* Neutral, because it doubles the WebDAV-related configuration surface
* Bad, because resource footprint grows (additional Teapot + at-least-one spawned JVM per test run)
* Bad, because it dilutes testbed focus — two ways to do the same thing with no clear "canonical" path
* Bad, because CI runtime grows, especially with the 60s first-spawn cost

## More Information

**When this decision should be revisited:**

* A concrete use case requires filesystem-level uid isolation (e.g., regulatory audit trails per user, compliance constraints, or onboarding a user group that cannot share a POSIX uid)
* A downstream adopter needs to demonstrate per-user sandboxing as part of their own deployment
* The OIDC transfer demonstration is no longer the primary value of the testbed, and multi-tenancy becomes the focus
* Teapot reaches a release state where multi-issuer support and stable-port reverse-proxy deployment are documented and tested

**Estimated migration effort (if/when adopted):**

| Phase | Activity | Effort | Risk |
|---|---|---|---|
| 1 | Add Teapot as an additive service alongside existing StoRM instances | 3 days | Low |
| 2 | Configure Keycloak group → POSIX username mapping in `teapot.ini` | 1 day | Low |
| 3 | Register a `TEAPOT1` RSE with a stable reverse-proxy URL | 0.5 day | Medium |
| 4 | Expand Keycloak audience mapper to include `teapot` / `TEAPOT1` | 0.5 day | Low |
| 5 | Add direct bearer-token test script against Teapot | 1 day | Medium |
| 6 | Add Rucio conveyor integration test to/from `TEAPOT1` | 2 days | Medium-High |
| 7 | Document the added service and migration notes | 1 day | Low |
| | **Total** | **~9 working days** | **Medium overall** |

**Artefacts to reuse when migrating:**

* The Keycloak realm (`config/keycloak/realm.json`) already issues WLCG-profile tokens with path-suffixed scopes — Teapot can consume these directly
* The fine-grained authorization policies (`config/storm-webdav/storm-application-policies.yml`) can be lifted into Teapot's per-user StoRM template with minor adjustment
* The test-script structure in `scripts/test-rucio-transfers.py` is already parameterized by RSE and can be extended with a `run_teapot_oidc_transfer_test` function

**Image availability:** The official `ghcr.io/intertwin-eu/teapot:latest` image requires authentication to a private GitHub Container Registry and cannot be pulled without interTwin org credentials. The DEB package at `https://github.com/interTwin-eu/teapot/releases/download/0.23.0/teapot_0.23.0-1_all.deb` is publicly available and can be used to build a custom image. Phase 1 of the migration effort requires building and publishing a custom image from the DEB package.

**References:**

* StoRM WebDAV: <https://github.com/italiangrid/storm-webdav>
* Teapot: <https://github.com/interTwin-eu/teapot>
* Teapot installation guide: <https://intertwin-eu.github.io/teapot/installation-guide/>
* Teapot configuration reference: <https://github.com/interTwin-eu/teapot/blob/main/CONFIGURATION.md>
* Current testbed troubleshooting guide: `docs/troubleshooting-oidc-transfers.md`
