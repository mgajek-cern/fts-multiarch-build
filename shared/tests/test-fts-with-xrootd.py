#!/usr/bin/env python3
"""
test-fts-with-xrootd.py — FTS3 GSI TPC test (xrd1 → xrd2) using the fts3
Python REST client.

Seeds the source file on xrd1 via xrdcp from inside the FTS container,
so the test is fully self-contained regardless of runtime (compose/k8s)
and does not depend on the lifecycle postStart hook having run.

Typical invocations:
    # Compose
    docker exec compose-fts-1 bash -c "pytest /tests/test-fts-with-xrootd.py"

    # Kubernetes
    kubectl -n rucio-testbed exec deploy/fts -- bash -c "pytest /tests/test-fts-with-xrootd.py"
"""

import datetime
import json
import logging
import os

import pytest

from testbed import poll_fts_job, xrd_exists, xrd_read, xrd_seed

try:
    import fts3.rest.client.easy as fts3
except ImportError:
    pytest.skip("fts3 module not available", allow_module_level=True)


log = logging.getLogger("test-fts-with-xrootd")

# ── Configuration via env ─────────────────────────────────────────────────
FTS = os.environ.get("FTS", "https://localhost:8446")
CERT = os.environ.get("CERT", "/etc/grid-security/hostcert.pem")
KEY = os.environ.get("KEY", "/etc/grid-security/hostkey.pem")

SRC_HOST = os.environ.get("SRC_HOST", "xrd1")
DST_HOST = os.environ.get("DST_HOST", "xrd2")
SRC_PATH = os.environ.get("SRC_PATH", "/rucio/fts-test-file")
DST_PATH = os.environ.get("DST_PATH", "/rucio/fts-test-file-copy")

SRC = os.environ.get("SRC", f"root://{SRC_HOST}/{SRC_PATH}")
DST = os.environ.get("DST", f"root://{DST_HOST}/{DST_PATH}")

SEED_CONTENT = "fts-test\n"


# ── Fixtures ──────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def seeded_source():
    """Seed the source file on xrd1 via xrdcp before any transfer test."""
    log.info("=== Seeding source file ===")
    log.info("  Target: %s", SRC)
    xrd_seed(SRC, SEED_CONTENT)
    yield SRC


@pytest.fixture(scope="session")
def context():
    log.info("=== Connecting to FTS at %s ===", FTS)
    return fts3.Context(endpoint=FTS, ucert=CERT, ukey=KEY, verify=True)


@pytest.fixture(scope="session")
def delegated_context(context):
    whoami = fts3.whoami(context)
    log.info("whoami: %s", json.dumps(whoami, indent=2))
    log.info("  DN:      %s", whoami["user_dn"])
    log.info("  is_root: %s", whoami["is_root"])
    log.info("  method:  %s", whoami["method"])
    assert "user_dn" in whoami and whoami["user_dn"]
    log.info("Delegating proxy (lifetime=1h)...")
    fts3.delegate(context, lifetime=datetime.timedelta(hours=1), force=True)
    log.info("✓ Delegation OK")
    return context


@pytest.fixture(scope="session")
def submitted_job(delegated_context, seeded_source):
    log.info("=== Submitting transfer ===")
    log.info("  %s -> %s", SRC, DST)
    transfer = fts3.new_transfer(SRC, DST)
    job_id = fts3.submit(
        delegated_context,
        fts3.new_job([transfer], overwrite=True, verify_checksum=False),
    )
    log.info("✓ Job submitted: %s", job_id)
    assert job_id is not None
    return job_id


# ── Tests ─────────────────────────────────────────────────────────────────
def test_whoami(context):
    whoami = fts3.whoami(context)
    assert "user_dn" in whoami and isinstance(whoami["user_dn"], str)
    assert "method" in whoami
    log.info("whoami response keys: %s", list(whoami.keys()))


def test_source_seeded(seeded_source):
    """Source file is accessible on xrd1 after seeding."""
    assert xrd_exists(seeded_source), (
        f"Source {seeded_source} not reachable via xrdfs after seeding"
    )
    log.info("  ✓ Source file confirmed present")


def test_delegate(delegated_context):
    assert delegated_context is not None
    log.info("✓ delegated_context fixture established")


def test_submit_job(submitted_job):
    assert submitted_job is not None
    log.info("✓ Job ID present: %s", submitted_job)


def test_job_lifecycle(delegated_context, submitted_job):
    state = poll_fts_job(delegated_context, submitted_job)
    assert state in {"FINISHED", "FAILED", "CANCELED", "FINISHEDDIRTY"}, (
        "Job never reached a terminal state"
    )
    assert state == "FINISHED", f"Job failed with state={state}"


def test_replica_on_dst(submitted_job):
    """Transferred file exists on xrd2 with correct content."""
    log.info("=== Verifying replica ===")
    assert xrd_exists(DST), f"Replica not found at {DST}"
    content = xrd_read(DST)
    assert content == SEED_CONTENT, (
        f"Content mismatch at {DST}: expected {SEED_CONTENT!r}, got {content!r}"
    )
    log.info("  ✓ Replica confirmed with correct content")
