#!/usr/bin/env python3
"""
test-fts-with-webdav.py — FTS3 WebDAV TPC test using X.509 GSI auth.

Covers two transfer scenarios:
  - xrd1 → webdav1  (XRootD GSI source, WebDAV destination)
  - webdav1 → xrd2  (WebDAV source, XRootD GSI destination)

Runs from inside the fts container, which has the GSI proxy, xrdcp,
xrdfs, and network access to xrd1/xrd2/webdav1/webdav2.

Typical invocations:
    # Compose
    docker exec compose-fts-1 bash -c "pytest /tests/test-fts-with-webdav.py"

    # Kubernetes
    kubectl -n rucio-testbed exec deploy/fts -- bash -c "pytest /tests/test-fts-with-webdav.py"
"""

import datetime
import logging
import os

import pytest

from testbed import (
    CURL_OK_EXITS,
    _run,
    poll_fts_job,
    xrd_exists,
    xrd_seed,
)

try:
    import fts3.rest.client.easy as fts3
except ImportError:
    pytest.skip("fts3 module not available", allow_module_level=True)


log = logging.getLogger("test-fts-with-webdav")

# ── Configuration via env ─────────────────────────────────────────────────
FTS = os.environ.get("FTS", "https://localhost:8446")
CERT = os.environ.get("CERT", "/etc/grid-security/hostcert.pem")
KEY = os.environ.get("KEY", "/etc/grid-security/hostkey.pem")
CACERT = os.environ.get("CACERT", "/etc/grid-security/certificates/rucio_ca.pem")

WEBDAV1_HOST = os.environ.get("WEBDAV1_HOST", "webdav1")
WEBDAV1_PORT = int(os.environ.get("WEBDAV1_PORT", "443").split(":")[-1])
WEBDAV1_BASE = f"https://{WEBDAV1_HOST}:{WEBDAV1_PORT}"

XRD1_HOST = os.environ.get("XRD1_HOST", "xrd1")
XRD2_HOST = os.environ.get("XRD2_HOST", "xrd2")

XRD_SRC_PATH = "/rucio/fts-test-file"
XRD_DST_PATH = "/rucio/fts-test-file-from-webdav"
WEBDAV_FILE_PATH = "/fts-test-file-from-xrd1"

XRD_SRC_URL = f"root://{XRD1_HOST}/{XRD_SRC_PATH}"
XRD_DST_URL = f"root://{XRD2_HOST}/{XRD_DST_PATH}"
WEBDAV_SRC_URL = f"davs://{WEBDAV1_HOST}:{WEBDAV1_PORT}{WEBDAV_FILE_PATH}"
WEBDAV_DST_URL = f"davs://{WEBDAV1_HOST}:{WEBDAV1_PORT}{WEBDAV_FILE_PATH}"

SEED_CONTENT = "fts-test\n"


# ── WebDAV-specific curl helpers ──────────────────────────────────────────
def webdav_curl(*args: str) -> str:
    """Run curl against WebDAV with client cert auth."""
    cmd = ["curl", "-sk", "--cert", CERT, "--key", KEY, "--cacert", CACERT] + list(args)
    result = _run(cmd, ok_exits=CURL_OK_EXITS)
    return result.stdout.decode(errors="replace")


def webdav_status(*args: str) -> str:
    """Return HTTP status code from a WebDAV curl call via header dump."""
    body = webdav_curl("-D", "-", *args)
    for line in body.splitlines():
        if line.startswith("HTTP/"):
            return line.split()[1]
    return "000"


# ── Fixtures ──────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def context():
    log.info("=== Connecting to FTS at %s ===", FTS)
    return fts3.Context(endpoint=FTS, ucert=CERT, ukey=KEY, verify=True)


@pytest.fixture(scope="session")
def delegated_context(context):
    whoami = fts3.whoami(context)
    log.info("  DN: %s  method: %s", whoami["user_dn"], whoami["method"])
    log.info("=== Delegating proxy (lifetime=1h) ===")
    fts3.delegate(context, lifetime=datetime.timedelta(hours=1), force=True)
    log.info("  ✓ Delegation OK")
    return context


@pytest.fixture(scope="session")
def seeded_source(delegated_context):
    """Ensure xrd1 and webdav1 are seeded before any transfer test."""
    # Wait for webdav1 to be ready
    import time

    for i in range(1, 21):
        code = webdav_status("-X", "PROPFIND", "-H", "Depth: 0", f"{WEBDAV1_BASE}/")
        if code in ("200", "207"):
            log.info("  ✓ webdav1 ready (HTTP %s)", code)
            break
        log.info("  [%d] webdav1 not ready (HTTP %s) — waiting", i, code)
        time.sleep(3)
    else:
        raise RuntimeError("webdav1 did not become ready")

    log.info("=== Ensuring seeded test data ===")
    if not xrd_exists(XRD_SRC_URL):
        log.info("  Seeding xrd1 via xrdcp")
        xrd_seed(XRD_SRC_URL, SEED_CONTENT)

    log.info("  Seeding webdav1 via PUT")
    webdav_curl(
        "-X", "PUT", "--data-binary", SEED_CONTENT, f"{WEBDAV1_BASE}{WEBDAV_FILE_PATH}"
    )

    assert xrd_exists(XRD_SRC_URL), f"Failed to seed {XRD_SRC_URL}"
    log.info("  ✓ Test data ready")
    yield XRD_SRC_URL


# ── Tests ─────────────────────────────────────────────────────────────────
def test_webdav1_reachable():
    """webdav1 PROPFIND returns 200 or 207."""
    code = webdav_status("-X", "PROPFIND", "-H", "Depth: 0", f"{WEBDAV1_BASE}/")
    assert code in ("200", "207"), f"webdav1 unreachable: HTTP {code}"
    log.info("  ✓ webdav1 reachable (HTTP %s)", code)


def test_whoami(context):
    """FTS identity and delegation ID are present."""
    whoami = fts3.whoami(context)
    assert whoami.get("user_dn")
    assert whoami.get("delegation_id")
    log.info("  ✓ FTS identity: %s", whoami["user_dn"])


def test_delegate(delegated_context):
    assert delegated_context is not None
    log.info("  ✓ Delegation fixture established")


def test_xrd_to_webdav(delegated_context, seeded_source):
    """xrd1 → webdav1: XRootD GSI source to WebDAV destination."""
    log.info("=== xrd1 → webdav1 ===")
    log.info("  %s -> %s", XRD_SRC_URL, WEBDAV_DST_URL)
    transfer = fts3.new_transfer(XRD_SRC_URL, WEBDAV_DST_URL)
    job_id = fts3.submit(delegated_context, fts3.new_job([transfer], overwrite=True))
    log.info("  Job ID: %s", job_id)
    state = poll_fts_job(delegated_context, job_id)
    assert state == "FINISHED", f"xrd1→webdav1 failed: state={state}"
    log.info("  ✓ xrd1 → webdav1 FINISHED")


def test_webdav_file_exists(delegated_context, seeded_source):
    """Transferred file is accessible on webdav1 via GET."""
    log.info("=== Verifying file on webdav1 ===")
    code = webdav_status(f"{WEBDAV1_BASE}{WEBDAV_FILE_PATH}")
    assert code == "200", (
        f"File not accessible on webdav1: GET {WEBDAV1_BASE}{WEBDAV_FILE_PATH} → HTTP {code}"
    )
    log.info("  ✓ File confirmed on webdav1 (HTTP %s)", code)


def test_webdav_to_xrd(delegated_context, seeded_source):
    """webdav1 → xrd2: WebDAV source to XRootD GSI destination."""
    log.info("=== webdav1 → xrd2 ===")
    log.info("  %s -> %s", WEBDAV_SRC_URL, XRD_DST_URL)
    transfer = fts3.new_transfer(WEBDAV_SRC_URL, XRD_DST_URL)
    job_id = fts3.submit(delegated_context, fts3.new_job([transfer], overwrite=True))
    log.info("  Job ID: %s", job_id)
    state = poll_fts_job(delegated_context, job_id)
    assert state == "FINISHED", f"webdav1→xrd2 failed: state={state}"
    log.info("  ✓ webdav1 → xrd2 FINISHED")


def test_xrd2_replica(delegated_context, seeded_source):
    """xrd2 has the file transferred from webdav1."""
    log.info("=== Verifying xrd2 replica ===")
    assert xrd_exists(XRD_DST_URL), f"Replica not found at {XRD_DST_URL}"
    log.info("  ✓ Replica confirmed on xrd2")


def test_webdav_listing():
    """webdav1 PROPFIND listing contains the transferred file."""
    log.info("=== webdav1 PROPFIND listing ===")
    body = webdav_curl("-X", "PROPFIND", "-H", "Depth: 1", f"{WEBDAV1_BASE}/")
    assert "fts-test-file-from-xrd1" in body, (
        f"Transferred file not found in webdav1 PROPFIND listing:\n{body[:500]}"
    )
    log.info("  ✓ fts-test-file-from-xrd1 visible in webdav1 listing")
