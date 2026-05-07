---
status: accepted
date: 2026-04-20
decision-makers: testbed maintainers
consulted: /
informed: /
---

# Prioritize Protocol Fidelity over Storage System Realism

## Context and Problem Statement

To validate Rucio and FTS3 OIDC orchestration, the testbed requires functional storage endpoints. We must decide between deploying "thin" protocol-only servers (MinIO, XRootD-standalone, Apache mod_dav) or "thick" production-grade Storage Elements (dCache, EOS, CTA).

The "thick" systems represent the reality of WLCG Tier-1/2 sites but introduce significant deployment complexity, resource overhead, and maintenance burdens that may obscure the primary goal: testing the OIDC transfer chain.

## Decision Drivers

* Maintain a "laptop-friendly" resource footprint (Low RAM/CPU).
* Ensure full compatibility with `linux/arm64` (Apple Silicon) for all components.
* Provide 100% protocol-accurate responses for XRootD, HTTP-TPC, and S3.
* Keep the bootstrap time under 5 minutes for CI/CD efficiency.

## Considered Options

* **Thin Backends (Chosen):** Standalone protocol servers (StoRM WebDAV, XRootD, MinIO).
* **Thick Backends:** Distributed storage systems (dCache, EOS).
* **Hybrid:** One "thick" system alongside several "thin" systems.

## Decision Outcome

Chosen option: **Thin Backends (Protocol Fidelity)**, because the core complexity of modern WLCG data management lies in the OIDC delegation and TPC orchestration, which can be fully exercised against protocol-compliant "thin" servers. Managing the internal state (pools, tape-backends) of a system like dCache adds "20% realism for 200% more effort."

### Consequences

* **Good:** The testbed remains highly portable and supports ARM64 natively.
* **Good:** CI/CD pipelines can spin up the entire stack deterministically.
* **Good:** 100% of OIDC token exchange and audience validation logic is preserved.
* **Bad:** We cannot simulate "Nearline" (Tape) states or internal storage failures (pool exhaustion, mover timeouts).
* **Bad:** Lack of a global namespace/federation layer (e.g., XRootD redirectors).

### Confirmation

Compliance is confirmed by:
* `./shared/tests/test-fts-with-storm-webdav.py` (Validates HTTP-TPC protocol).
* `./shared/tests/test-fts-with-s3.py` (Validates S3 protocol).
* OIDC token forwarding successfully reaching the StoRM/XRootD logs.
