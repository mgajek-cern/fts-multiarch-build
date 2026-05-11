#!/usr/bin/env python3
"""
test-teapot.py — WebDAV access test via Teapot with OIDC bearer tokens.

Runs from inside the rucio-client container, which has network access to
Keycloak and Teapot.

Typical invocations:
    # Compose
    docker exec compose-rucio-client-1 bash -c "pytest /tests/test-teapot.py -v"

    # Kubernetes
    kubectl -n rucio-testbed exec deploy/rucio-client -- bash -c "pytest /tests/test-teapot.py -v"
"""

import logging
import os

import pytest
import requests
import urllib3

from testbed import fetch_token_password, wait_for_http

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("test-teapot")

# ── Configuration via env ─────────────────────────────────────────────────
KEYCLOAK_URL = os.environ.get(
    "KEYCLOAK_URL",
    "https://keycloak:8443/realms/rucio/protocol/openid-connect/token",
)
KEYCLOAK_CLIENT_ID = os.environ.get("KEYCLOAK_CLIENT_ID", "rucio-oidc")
KEYCLOAK_CLIENT_SECRET = os.environ.get("KEYCLOAK_CLIENT_SECRET", "rucio-oidc-secret")
KEYCLOAK_USERNAME = os.environ.get("KEYCLOAK_USERNAME", "randomaccount")
KEYCLOAK_PASSWORD = os.environ.get("KEYCLOAK_PASSWORD", "secret")

TEAPOT_HOST = os.environ.get("TEAPOT_HOST", "teapot")
TEAPOT_URL = f"https://{TEAPOT_HOST}:8081"

STORAGE_AREA = "/data"
TEST_FILE_PATH = f"{STORAGE_AREA}/teapot-test-file"
TEST_FILE_CONTENT = b"teapot-test\n"


# ── Helpers ───────────────────────────────────────────────────────────────
def teapot_propfind(
    path: str, token: str, depth: str = "1", timeout: int = 120
) -> requests.Response:
    return requests.request(
        "PROPFIND",
        f"{TEAPOT_URL}{path}",
        headers={"Authorization": f"Bearer {token}", "Depth": depth},
        verify=False,
        timeout=timeout,
    )


def teapot_put(path: str, token: str, content: bytes) -> requests.Response:
    return requests.put(
        f"{TEAPOT_URL}{path}",
        headers={"Authorization": f"Bearer {token}"},
        data=content,
        verify=False,
        timeout=30,
    )


def teapot_get(path: str, token: str) -> requests.Response:
    return requests.get(
        f"{TEAPOT_URL}{path}",
        headers={"Authorization": f"Bearer {token}"},
        verify=False,
        timeout=30,
    )


def teapot_delete(path: str, token: str) -> requests.Response:
    return requests.delete(
        f"{TEAPOT_URL}{path}",
        headers={"Authorization": f"Bearer {token}"},
        verify=False,
        timeout=30,
    )


# ── Fixtures ──────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def token():
    log.info("=== Fetching token from Keycloak (scope=openid required) ===")
    t = fetch_token_password(
        KEYCLOAK_URL,
        KEYCLOAK_CLIENT_ID,
        KEYCLOAK_CLIENT_SECRET,
        KEYCLOAK_USERNAME,
        KEYCLOAK_PASSWORD,
        scope="openid",
    )
    assert t, "Token is empty"
    log.info("  ✓ Token obtained for %s", KEYCLOAK_USERNAME)
    return t


@pytest.fixture(scope="session")
def teapot_ready(token):
    """Wait for Teapot HTTPS endpoint, then warm up Storm-WebDAV."""
    wait_for_http(
        f"{TEAPOT_URL}/docs",
        "teapot /docs",
        expected_codes={200},
    )
    # First PROPFIND triggers Storm-WebDAV cold start (~30s); subsequent
    # requests reuse the running instance.
    log.info("=== Warming up Storm-WebDAV instance via first PROPFIND ===")
    # Cold-start on CI ARM64 runners can take >120s — use 240s for the warm-up
    resp = teapot_propfind(STORAGE_AREA + "/", token, timeout=240)
    assert resp.status_code == 207, (
        f"Teapot warm-up PROPFIND returned HTTP {resp.status_code}: {resp.text[:200]}"
    )
    log.info("  ✓ Storm-WebDAV instance running (HTTP 207)")
    return True


# ── Tests ─────────────────────────────────────────────────────────────────
def test_teapot_healthy():
    resp = requests.get(f"{TEAPOT_URL}/docs", verify=False, timeout=10)
    assert resp.ok, f"Teapot /docs health check failed: HTTP {resp.status_code}"
    log.info("  ✓ Teapot healthy (HTTP %s)", resp.status_code)


def test_token_obtained(token):
    assert token
    log.info("  ✓ Token present")


def test_storage_area_listing(token, teapot_ready):
    """PROPFIND on the root storage area returns 207 Multi-Status."""
    resp = teapot_propfind(STORAGE_AREA + "/", token)
    assert resp.status_code == 207, (
        f"PROPFIND {STORAGE_AREA}/ returned HTTP {resp.status_code}"
    )
    assert "<d:multistatus" in resp.text, "Response is not WebDAV XML"
    log.info("  ✓ Storage area listing OK (HTTP 207)")


def test_file_upload(token, teapot_ready):
    """PUT a test file into the storage area."""
    resp = teapot_put(TEST_FILE_PATH, token, TEST_FILE_CONTENT)
    assert resp.status_code in {200, 201, 204}, (
        f"PUT {TEST_FILE_PATH} returned HTTP {resp.status_code}"
    )
    log.info("  ✓ File uploaded (HTTP %s)", resp.status_code)


def test_file_download(token, teapot_ready):
    """GET the uploaded file and verify content."""
    resp = teapot_get(TEST_FILE_PATH, token)
    assert resp.status_code == 200, (
        f"GET {TEST_FILE_PATH} returned HTTP {resp.status_code}"
    )
    assert resp.content == TEST_FILE_CONTENT, (
        f"Content mismatch: expected {TEST_FILE_CONTENT!r}, got {resp.content!r}"
    )
    log.info("  ✓ File content verified (HTTP 200)")


def test_file_visible_in_listing(token, teapot_ready):
    """PROPFIND on the storage area shows the uploaded file."""
    resp = teapot_propfind(STORAGE_AREA + "/", token)
    assert resp.status_code == 207
    assert "teapot-test-file" in resp.text, (
        f"Uploaded file not visible in PROPFIND response:\n{resp.text[:500]}"
    )
    log.info("  ✓ Uploaded file visible in listing")


def test_unauthenticated_request_rejected():
    """Request without a token must be rejected with 401 or 403."""
    resp = requests.request(
        "PROPFIND",
        f"{TEAPOT_URL}{STORAGE_AREA}/",
        headers={"Depth": "1"},
        verify=False,
        timeout=10,
    )
    assert resp.status_code in {401, 403}, (
        f"Expected 401/403 without token, got HTTP {resp.status_code}"
    )
    log.info(
        "  ✓ Unauthenticated request correctly rejected (HTTP %s)", resp.status_code
    )


def test_file_delete(token, teapot_ready):
    """DELETE the test file and confirm it's gone."""
    resp = teapot_delete(TEST_FILE_PATH, token)
    assert resp.status_code in {200, 204}, (
        f"DELETE {TEST_FILE_PATH} returned HTTP {resp.status_code}"
    )
    get_resp = teapot_get(TEST_FILE_PATH, token)
    assert get_resp.status_code == 404, (
        f"File still accessible after DELETE: HTTP {get_resp.status_code}"
    )
    log.info("  ✓ File deleted and confirmed gone")
