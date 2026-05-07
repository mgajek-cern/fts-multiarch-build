#!/usr/bin/env python3
"""
test-fts-with-s3.py — FTS3 S3/MinIO transfer tests using X.509 GSI auth.

Covers three transfer scenarios:
  - xrd1   → MinIO1  (XRootD GSI source, S3 destination)
  - MinIO1  → xrd2   (S3 source, XRootD GSI destination)
  - MinIO1  → MinIO2 (S3-to-S3)

Runs from inside the fts container, which has the GSI proxy, xrdcp,
xrdfs, curl with the client cert, and network access to all endpoints.

Typical invocations:
    # Compose
    docker exec compose-fts-1 bash -c "pytest /tests/test-fts-with-s3.py"

    # Kubernetes
    kubectl -n rucio-testbed exec deploy/fts -- bash -c "pytest /tests/test-fts-with-s3.py"
"""

import datetime
import json
import logging
import os

import pytest

from testbed import (
    _run,
    fts_curl,
    fts_curl_code,
    poll_fts_job,
    xrd_exists,
    xrd_seed,
)

try:
    import fts3.rest.client.easy as fts3
except ImportError:
    pytest.skip("fts3 module not available", allow_module_level=True)


log = logging.getLogger("test-fts-with-s3")

# ── Configuration via env ─────────────────────────────────────────────────
FTS = os.environ.get("FTS", "https://localhost:8446")
CERT = os.environ.get("CERT", "/etc/grid-security/hostcert.pem")
KEY = os.environ.get("KEY", "/etc/grid-security/hostkey.pem")

MINIO1_HOST = os.environ.get("MINIO1_HOST", "minio1")
MINIO2_HOST = os.environ.get("MINIO2_HOST", "minio2")
MINIO_PORT = int(os.environ.get("MINIO_PORT", "9000"))
MINIO_USER = os.environ.get("MINIO_USER", "minioadmin")
MINIO_PASSWORD = os.environ.get("MINIO_PASSWORD", "minioadmin")
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "fts-test")

XRD1_HOST = os.environ.get("XRD1_HOST", "xrd1")
XRD2_HOST = os.environ.get("XRD2_HOST", "xrd2")
XRD_SRC_PATH = os.environ.get("XRD_SRC_PATH", "/rucio/fts-test-file")
XRD_DST_PATH = os.environ.get("XRD_DST_PATH", "/rucio/fts-test-file-from-s3")

XRD_SRC_URL = f"root://{XRD1_HOST}/{XRD_SRC_PATH}"
XRD_DST_URL = f"root://{XRD2_HOST}/{XRD_DST_PATH}"

S3_SEED_URL = f"s3://{MINIO1_HOST}:{MINIO_PORT}/{MINIO_BUCKET}/fts-test-file"
S3_DST1_URL = f"s3://{MINIO1_HOST}:{MINIO_PORT}/{MINIO_BUCKET}/fts-test-file-from-xrd1"
S3_DST2_URL = f"s3://{MINIO2_HOST}:{MINIO_PORT}/{MINIO_BUCKET}/fts-test-file-copy"

SEED_CONTENT = "fts-test\n"


# ── Local helpers ─────────────────────────────────────────────────────────
def mc_ls(host: str) -> str:
    """List bucket contents using mc."""
    cmd = [
        "bash",
        "-c",
        f"mc alias set local http://{host}:{MINIO_PORT} {MINIO_USER} {MINIO_PASSWORD} --quiet && "
        f"mc ls local/{MINIO_BUCKET}/",
    ]
    return _run(cmd).stdout.decode()


# ── Fixtures ──────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def context():
    log.info("=== Connecting to FTS at %s ===", FTS)
    return fts3.Context(endpoint=FTS, ucert=CERT, ukey=KEY, verify=True)


@pytest.fixture(scope="session")
def delegated_context(context):
    """Delegate proxy and register S3 credentials (once per session)."""
    whoami = fts3.whoami(context)
    log.info("  DN: %s  method: %s", whoami["user_dn"], whoami["method"])

    log.info("=== Delegating proxy (lifetime=1h) ===")
    fts3.delegate(context, lifetime=datetime.timedelta(hours=1), force=True)
    log.info("  ✓ Delegation OK")

    log.info("=== Registering S3 credentials ===")
    for storage in (f"S3:{MINIO1_HOST}", f"S3:{MINIO2_HOST}"):
        fts_curl("-X", "DELETE", f"{FTS}/config/cloud_storage/{storage}")

        code = fts_curl_code(
            "-X",
            "POST",
            f"{FTS}/config/cloud_storage",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps({"storage_name": storage}),
        )
        log.info("  register %s: HTTP %s", storage, code)

        grant_code = fts_curl_code(
            "-X",
            "POST",
            f"{FTS}/config/cloud_storage/{storage}",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps(
                {
                    "vo_name": "*",
                    "access_token": MINIO_USER,
                    "access_token_secret": MINIO_PASSWORD,
                }
            ),
        )
        assert grant_code in ("200", "201"), (
            f"S3 credential grant for {storage} failed: HTTP {grant_code}"
        )
        log.info("  ✓ S3 credentials registered for %s (HTTP %s)", storage, grant_code)

    return context


@pytest.fixture(scope="session")
def seeded_xrd1(delegated_context):
    """Seed the source file on xrd1 via xrdcp."""
    log.info("=== Seeding source file on xrd1 ===")
    xrd_seed(XRD_SRC_URL, SEED_CONTENT)
    yield XRD_SRC_URL


# ── Tests ─────────────────────────────────────────────────────────────────
def test_fts_whoami(context):
    """FTS is reachable and returns a valid identity."""
    whoami = fts3.whoami(context)
    assert "user_dn" in whoami and whoami["user_dn"]
    log.info("  ✓ FTS identity: %s", whoami["user_dn"])


def test_source_seeded(seeded_xrd1):
    """Source file is accessible on xrd1 after seeding."""
    assert xrd_exists(seeded_xrd1), f"Source {seeded_xrd1} not found after seeding"
    log.info("  ✓ Source confirmed on xrd1")


def test_s3_xrd_to_minio1(delegated_context, seeded_xrd1):
    """xrd1 → MinIO1: XRootD GSI source to S3 destination."""
    log.info("=== S3: xrd1 → MinIO1 ===")
    transfer = fts3.new_transfer(XRD_SRC_URL, S3_DST1_URL, checksum=None)
    job_id = fts3.submit(
        delegated_context,
        fts3.new_job([transfer], overwrite=True, verify_checksum=False),
    )
    log.info("  Job ID: %s", job_id)
    state = poll_fts_job(delegated_context, job_id)
    assert state == "FINISHED", f"xrd1→MinIO1 failed: state={state}"
    log.info("  ✓ xrd1 → MinIO1 FINISHED")


def test_s3_minio1_to_xrd2(delegated_context, seeded_xrd1):
    """MinIO1 → xrd2: S3 source to XRootD GSI destination."""
    log.info("=== S3: MinIO1 → xrd2 ===")
    transfer = fts3.new_transfer(S3_SEED_URL, XRD_DST_URL, checksum=None)
    job_id = fts3.submit(
        delegated_context,
        fts3.new_job([transfer], overwrite=True, verify_checksum=False),
    )
    log.info("  Job ID: %s", job_id)
    state = poll_fts_job(delegated_context, job_id)
    assert state == "FINISHED", f"MinIO1→xrd2 failed: state={state}"
    log.info("  ✓ MinIO1 → xrd2 FINISHED")


def test_s3_minio1_to_minio2(delegated_context, seeded_xrd1):
    """MinIO1 → MinIO2: S3-to-S3 transfer."""
    log.info("=== S3: MinIO1 → MinIO2 ===")
    transfer = fts3.new_transfer(S3_SEED_URL, S3_DST2_URL, checksum=None)
    job_id = fts3.submit(
        delegated_context,
        fts3.new_job([transfer], overwrite=True, verify_checksum=False),
    )
    log.info("  Job ID: %s", job_id)
    state = poll_fts_job(delegated_context, job_id)
    assert state == "FINISHED", f"MinIO1→MinIO2 failed: state={state}"
    log.info("  ✓ MinIO1 → MinIO2 FINISHED")


def test_xrd2_replica(delegated_context):
    """xrd2 has the file transferred from MinIO1."""
    log.info("=== Verifying xrd2 replica ===")
    assert xrd_exists(XRD_DST_URL), f"Replica not found at {XRD_DST_URL}"
    log.info("  ✓ Replica confirmed on xrd2")


def test_minio1_bucket(delegated_context):
    """MinIO1 fts-test bucket contains the file transferred from xrd1."""
    log.info("=== MinIO1 bucket contents ===")
    raw = mc_ls(MINIO1_HOST)
    assert "fts-test-file-from-xrd1" in raw, (
        f"fts-test-file-from-xrd1 not found in MinIO1/{MINIO_BUCKET}\n{raw[:500]}"
    )
    log.info("  ✓ MinIO1 bucket contains fts-test-file-from-xrd1")


def test_minio2_bucket(delegated_context):
    """MinIO2 fts-test bucket contains the file copied from MinIO1."""
    log.info("=== MinIO2 bucket contents ===")
    raw = mc_ls(MINIO2_HOST)
    assert "fts-test-file-copy" in raw, (
        f"fts-test-file-copy not found in MinIO2/{MINIO_BUCKET}\n{raw[:500]}"
    )
    log.info("  ✓ MinIO2 bucket contains fts-test-file-copy")
