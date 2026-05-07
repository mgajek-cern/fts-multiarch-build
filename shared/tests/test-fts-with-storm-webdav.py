#!/usr/bin/env python3
"""
test-fts-with-storm-webdav.py — HTTP TPC test using StoRM WebDAV (storm1 → storm2)
with OIDC bearer tokens from Keycloak.

Runs from inside the fts-oidc container, which has network access to
Keycloak, storm1, and storm2, and the CA trust anchors already configured.

Typical invocations:
    # Compose
    docker exec compose-fts-oidc-1 bash -c "pytest /tests/test-fts-with-storm-webdav.py"

    # Kubernetes
    kubectl -n rucio-testbed exec deploy/fts-oidc -- bash -c "pytest /tests/test-fts-with-storm-webdav.py"
"""

import importlib.util
import logging
import os
import time

import pytest
import requests
import urllib3

from testbed import (
    RUNTIME,
    fetch_token_password,
    poll_fts_job_http,
    svc_exec,
    wait_for_http,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

if importlib.util.find_spec("fts3") is None:
    pytest.skip("fts3 module not available", allow_module_level=True)


log = logging.getLogger("test-fts-with-storm-webdav")

# ── Configuration via env ─────────────────────────────────────────────────
FTS = os.environ.get("FTS", "https://localhost:8446")

KEYCLOAK_URL = os.environ.get(
    "KEYCLOAK_URL",
    "https://keycloak:8443/realms/rucio/protocol/openid-connect/token",
)
KEYCLOAK_CLIENT_ID = os.environ.get("KEYCLOAK_CLIENT_ID", "rucio-oidc")
KEYCLOAK_CLIENT_SECRET = os.environ.get("KEYCLOAK_CLIENT_SECRET", "rucio-oidc-secret")
KEYCLOAK_USERNAME = os.environ.get("KEYCLOAK_USERNAME", "randomaccount")
KEYCLOAK_PASSWORD = os.environ.get("KEYCLOAK_PASSWORD", "secret")

STORM1_HOST = os.environ.get("STORM1_HOST", "storm1")
STORM2_HOST = os.environ.get("STORM2_HOST", "storm2")
STORM1_HTTPS = f"https://{STORM1_HOST}:8443"
STORM2_HTTPS = f"https://{STORM2_HOST}:8443"
STORM1_HTTP = f"http://{STORM1_HOST}:8085"
STORM2_HTTP = f"http://{STORM2_HOST}:8085"

SRC_FS_PATH = "/storage/data/fts-test-file"
SRC_PATH = "/data/fts-test-file"
DST_PATH = "/data/fts-test-file-copy"

SRC_URL = f"http://{STORM1_HOST}:8085{SRC_PATH}"
DST_URL = f"davs://{STORM2_HOST}:8443{DST_PATH}"

SEED_CONTENT = b"fts-test\n"
HEALTH_PATH = "/.storm-webdav/actuator/health"


# ── Local helpers ─────────────────────────────────────────────────────────
def storm_get(url: str, token: str = None) -> requests.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return requests.get(url, headers=headers, verify=False, timeout=10)


def storm_propfind(url: str, token: str = None) -> requests.Response:
    headers = {"Depth": "1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.request("PROPFIND", url, headers=headers, verify=False, timeout=10)


def wait_for_token_auth(token: str, retries: int = 12, interval: int = 10) -> None:
    """Poll storm1 PROPFIND until JWKS warms up (HTTP 207)."""
    for i in range(1, retries + 1):
        resp = storm_propfind(f"{STORM1_HTTPS}/data/", token=token)
        if resp.status_code == 207:
            log.info("  ✓ StoRM token auth OK")
            return
        log.info("  [%d] HTTP %s — warming JWKS cache...", i, resp.status_code)
        time.sleep(interval)
    raise RuntimeError("StoRM token auth did not succeed within the retry window")


def seed_storm1_fs() -> None:
    svc_exec(
        "storm1",
        [
            "sh",
            "-c",
            f"echo 'fts-test' > {SRC_FS_PATH} && chown storm:storm {SRC_FS_PATH}",
        ],
        user="root",
    )
    log.info("  ✓ Seeded %s on storm1 filesystem", SRC_FS_PATH)


def prepare_storage_areas() -> None:
    if RUNTIME == "k8s":
        log.info("=== Storage areas managed by chart (fsGroup) ===")
        return
    for s in ("storm1", "storm2"):
        svc_exec(
            s,
            [
                "sh",
                "-c",
                "mkdir -p /storage/data && chown storm:storm /storage/data && chmod 755 /storage/data",
            ],
            user="root",
        )
        log.info("  ✓ storage area prepared on %s", s)


def storm1_to_storm2_check() -> None:
    out = (
        svc_exec(
            "storm1",
            [
                "curl",
                "-sk",
                f"http://{STORM2_HOST}:8085{HEALTH_PATH}",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
            ],
        )
        .decode()
        .strip()
    )
    if not out.startswith("2"):
        raise RuntimeError(f"storm1 → storm2 unreachable: HTTP {out}")
    log.info("  ✓ storm1 → storm2: HTTP %s", out)


# ── Fixtures ──────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def token():
    log.info("=== Fetching token from Keycloak ===")
    t = fetch_token_password(
        KEYCLOAK_URL,
        KEYCLOAK_CLIENT_ID,
        KEYCLOAK_CLIENT_SECRET,
        KEYCLOAK_USERNAME,
        KEYCLOAK_PASSWORD,
    )
    assert t, "Token is empty"
    log.info("  ✓ Token obtained for %s", KEYCLOAK_USERNAME)
    return t


@pytest.fixture(scope="session")
def seeded_source(token):
    prepare_storage_areas()
    wait_for_http(f"{STORM1_HTTP}{HEALTH_PATH}", "storm1 self", expected_codes={200})
    wait_for_http(f"{STORM2_HTTP}{HEALTH_PATH}", "storm2 self", expected_codes={200})
    wait_for_http(
        f"{STORM1_HTTP}{HEALTH_PATH}", "fts-oidc → storm1", expected_codes={200}
    )
    storm1_to_storm2_check()
    wait_for_token_auth(token)
    log.info("=== Seeding source file on storm1 ===")
    seed_storm1_fs()
    yield SRC_PATH


@pytest.fixture(scope="session")
def submitted_job(token, seeded_source):
    log.info("=== Submitting HTTP TPC Job ===")
    log.info("  %s -> %s", SRC_URL, DST_URL)
    job_body = {
        "files": [
            {
                "sources": [SRC_URL],
                "destinations": [DST_URL],
                "source_tokens": [token],
                "destination_tokens": [token],
            }
        ],
        "params": {"overwrite": True, "unmanaged_tokens": True},
    }
    resp = requests.post(
        f"{FTS}/jobs",
        json=job_body,
        headers={"Authorization": f"Bearer {token}"},
        verify=False,
        timeout=10,
    )
    resp.raise_for_status()
    job_id = resp.json()["job_id"]
    log.info("  ✓ Job submitted: %s", job_id)
    return job_id


# ── Tests ─────────────────────────────────────────────────────────────────
def test_storm1_healthy():
    resp = requests.get(f"{STORM1_HTTP}{HEALTH_PATH}", verify=False, timeout=5)
    assert resp.ok, f"storm1 health check failed: HTTP {resp.status_code}"
    log.info("  ✓ storm1 healthy (HTTP %s)", resp.status_code)


def test_storm2_healthy():
    resp = requests.get(f"{STORM2_HTTP}{HEALTH_PATH}", verify=False, timeout=5)
    assert resp.ok, f"storm2 health check failed: HTTP {resp.status_code}"
    log.info("  ✓ storm2 healthy (HTTP %s)", resp.status_code)


def test_token_obtained(token):
    assert token
    log.info("  ✓ Token present")


def test_source_seeded(seeded_source):
    resp = storm_get(f"{STORM1_HTTPS}{seeded_source}")
    assert resp.status_code == 200, (
        f"Source not accessible: GET {STORM1_HTTPS}{seeded_source} → HTTP {resp.status_code}"
    )
    log.info("  ✓ Source confirmed present (HTTP %s)", resp.status_code)


def test_job_submitted(submitted_job):
    assert submitted_job
    log.info("  ✓ Job ID: %s", submitted_job)


def test_job_lifecycle(token, submitted_job):
    state = poll_fts_job_http(FTS, submitted_job, token)
    assert state in {"FINISHED", "FAILED", "CANCELED", "FINISHEDDIRTY"}, (
        "Job never reached a terminal state"
    )
    assert state == "FINISHED", f"Transfer failed with state={state}"


def test_replica_on_storm2(submitted_job):
    log.info("=== Verifying storm2 result ===")
    resp = storm_get(f"{STORM2_HTTPS}{DST_PATH}")
    assert resp.status_code == 200, (
        f"Replica not found: GET {STORM2_HTTPS}{DST_PATH} → HTTP {resp.status_code}"
    )
    assert resp.content == SEED_CONTENT, (
        f"Content mismatch: expected {SEED_CONTENT!r}, got {resp.content!r}"
    )
    log.info("  ✓ Replica confirmed with correct content (HTTP %s)", resp.status_code)


def test_storm2_listing(submitted_job):
    log.info("=== storm2 /data/ listing ===")
    resp = storm_propfind(f"{STORM2_HTTPS}/data/")
    assert resp.status_code == 207, f"PROPFIND /data/ returned HTTP {resp.status_code}"
    assert "fts-test-file-copy" in resp.text, (
        f"Transferred file not found in PROPFIND response:\n{resp.text[:500]}"
    )
    log.info("  ✓ Transferred file visible in storm2 /data/ listing")
