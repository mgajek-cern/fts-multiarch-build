#!/usr/bin/env python3
"""
test-fts-with-xrootd-scitokens.py — XRootD SciTokens HTTP-TPC test
(xrd3 → xrd4) using bearer tokens from Keycloak.

Runs from inside the fts-oidc container, which has network access to
Keycloak, xrd3, and xrd4, and the CA trust anchors already configured.

Typical invocations:
    # Compose
    docker exec compose-fts-oidc-1 bash -c "pytest /tests/test-fts-with-xrootd-scitokens.py"

    # Kubernetes
    kubectl -n rucio-testbed exec deploy/fts-oidc -- bash -c "pytest /tests/test-fts-with-xrootd-scitokens.py"
"""

import importlib.util
import logging
import os
import time

import pytest
import requests
import urllib3

from testbed import (
    davs_exists,
    davs_read,
    davs_seed,
    decode_jwt_claims,
    fetch_token_client_credentials,
    poll_fts_job_http,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

if importlib.util.find_spec("fts3") is None:
    pytest.skip("fts3 module not available", allow_module_level=True)


log = logging.getLogger("test-fts-with-xrootd-scitokens")

# ── Configuration via env ─────────────────────────────────────────────────
FTS = os.environ.get("FTS", "https://localhost:8446")

KEYCLOAK_URL = os.environ.get(
    "KEYCLOAK_URL",
    "https://keycloak:8443/realms/rucio/protocol/openid-connect/token",
)
KEYCLOAK_CLIENT_ID = os.environ.get("KEYCLOAK_CLIENT_ID", "rucio-oidc")
KEYCLOAK_CLIENT_SECRET = os.environ.get("KEYCLOAK_CLIENT_SECRET", "rucio-oidc-secret")

SRC_HOST = os.environ.get("SRC_HOST", "xrd3")
DST_HOST = os.environ.get("DST_HOST", "xrd4")
SRC_BASE = os.environ.get("SRC_BASE", "/data")
DST_BASE = os.environ.get("DST_BASE", "/data")

SEED_CONTENT = "xrd-tpc-test\n"

_RUN_ID = int(time.time())
SRC_FILENAME = f"scitokens-test-{_RUN_ID}"
DST_FILENAME = f"scitokens-test-{_RUN_ID}-copy"

SRC_PATH = f"{SRC_BASE}/{SRC_FILENAME}"
DST_PATH = f"{DST_BASE}/{DST_FILENAME}"

SRC_URL = f"davs://{SRC_HOST}:1094{SRC_PATH}"
DST_URL = f"davs://{DST_HOST}:1094{DST_PATH}"

XRD_PORT = int(os.environ.get("XRD_PORT", "1094"))


# ── Local helper ──────────────────────────────────────────────────────────
def xrd_http_reachable(host: str) -> bool:
    """Return True if XRootD HTTP endpoint responds with any expected code."""
    try:
        r = requests.get(f"https://{host}:{XRD_PORT}/", verify=False, timeout=5)
        return r.status_code in (200, 401, 403, 404)
    except requests.RequestException:
        return False


# ── Fixtures ──────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def tokens():
    """Fetch FTS, source, and destination tokens from Keycloak."""
    log.info("=== Fetching tokens from Keycloak ===")
    fts_token = fetch_token_client_credentials(
        KEYCLOAK_URL,
        KEYCLOAK_CLIENT_ID,
        KEYCLOAK_CLIENT_SECRET,
        scope="openid fts",
        audience="fts-oidc",
    )
    src_token = fetch_token_client_credentials(
        KEYCLOAK_URL,
        KEYCLOAK_CLIENT_ID,
        KEYCLOAK_CLIENT_SECRET,
        scope="openid storage.read:/data",
        audience=SRC_HOST,
    )
    dst_token = fetch_token_client_credentials(
        KEYCLOAK_URL,
        KEYCLOAK_CLIENT_ID,
        KEYCLOAK_CLIENT_SECRET,
        scope="openid storage.modify:/data",
        audience=DST_HOST,
    )
    log.info("  ✓ fts_token obtained")
    claims = decode_jwt_claims(src_token)
    log.info("  src_token: iss=%s aud=%s", claims.get("iss"), claims.get("aud"))
    return {"fts": fts_token, "src": src_token, "dst": dst_token}


@pytest.fixture(scope="session")
def seeded_source(tokens):
    """Seed the source file on xrd3 via gfal-copy with a bearer token."""
    log.info("=== Seeding source file ===")
    log.info("  Target: %s", SRC_URL)
    davs_seed(SRC_URL, SEED_CONTENT, tokens["src"])
    yield SRC_URL


@pytest.fixture(scope="session")
def submitted_job(tokens, seeded_source):
    """Submit the TPC job to fts-oidc."""
    log.info("=== Submitting TPC job ===")
    log.info("  %s -> %s", SRC_URL, DST_URL)
    job_body = {
        "files": [
            {
                "sources": [SRC_URL],
                "destinations": [DST_URL],
                "source_tokens": [tokens["src"]],
                "destination_tokens": [tokens["dst"]],
            }
        ],
        "params": {
            "overwrite": True,
            "unmanaged_tokens": True,
            "verify_checksum": "none",
        },
    }
    resp = requests.post(
        f"{FTS}/jobs",
        json=job_body,
        headers={"Authorization": f"Bearer {tokens['fts']}"},
        verify=False,
        timeout=10,
    )
    resp.raise_for_status()
    job_id = resp.json()["job_id"]
    log.info("  ✓ Job submitted: %s", job_id)
    return job_id


# ── Tests ─────────────────────────────────────────────────────────────────
def test_xrd3_reachable():
    """xrd3 HTTPS endpoint is reachable from fts-oidc."""
    assert xrd_http_reachable(SRC_HOST), (
        f"https://{SRC_HOST}:{XRD_PORT} unreachable or XRootD HTTP not running"
    )
    log.info("  ✓ %s reachable", SRC_HOST)


def test_xrd4_reachable():
    """xrd4 HTTPS endpoint is reachable from fts-oidc."""
    assert xrd_http_reachable(DST_HOST), (
        f"https://{DST_HOST}:{XRD_PORT} unreachable or XRootD HTTP not running"
    )
    log.info("  ✓ %s reachable", DST_HOST)


def test_tokens_obtained(tokens):
    """All three tokens were issued by Keycloak."""
    for name in ("fts", "src", "dst"):
        assert tokens[name], f"Token '{name}' is empty"
        claims = decode_jwt_claims(tokens[name])
        assert "iss" in claims, f"Token '{name}' missing iss claim"
    log.info("  ✓ All tokens valid")


def test_source_seeded(seeded_source, tokens):
    """Source file is accessible on xrd3 after seeding."""
    log.info("Checking source accessible: %s", seeded_source)
    assert davs_exists(seeded_source, tokens["src"]), (
        f"Source {seeded_source} not reachable after seeding"
    )
    log.info("  ✓ Source file confirmed present")


def test_job_submitted(submitted_job):
    """FTS accepted the transfer job."""
    assert submitted_job
    log.info("  ✓ Job ID: %s", submitted_job)


def test_job_lifecycle(tokens, submitted_job):
    """Transfer reaches FINISHED within the polling window."""
    state = poll_fts_job_http(FTS, submitted_job, tokens["fts"])
    assert state in {"FINISHED", "FAILED", "CANCELED", "FINISHEDDIRTY"}, (
        "Job never reached a terminal state"
    )
    assert state == "FINISHED", f"Job failed with state={state}"


def test_replica_on_dst(submitted_job, tokens):
    """Replica exists on xrd4 with correct content after transfer."""
    log.info("=== Verifying replica ===")
    log.info("  Target: %s", DST_URL)
    assert davs_exists(DST_URL, tokens["dst"]), f"Replica not found at {DST_URL}"
    content = davs_read(DST_URL, tokens["dst"])
    assert content == SEED_CONTENT, (
        f"Content mismatch: expected {SEED_CONTENT!r}, got {content!r}"
    )
    log.info("  ✓ Replica confirmed with correct content")
