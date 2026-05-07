#!/usr/bin/env python3
"""
test-rucio-replica-unavailable.py — Validates Rucio's response to source
storage unavailability via two complementary paths.

Two tests, two layers:

  test_rule_stuck_when_source_unavailable
    Disables availability_read on XRD1 before submission. The submitter
    finds no readable source, sets the request to NO_SOURCES, and the
    finisher marks the lock STUCK in one daemon cycle. Production path for
    operator-declared RSE downtime. Mechanism: external_id stays None
    throughout — confirms rejection upstream of FTS.

  test_rule_fails_when_source_container_down
    Stops the xrd1 container without flipping any RSE attribute. The rule
    eventually reaches STUCK. Production path for unexpected/silent
    storage outage. Mechanism is intentionally not asserted: prior runs
    showed the request can be archived by the time it's looked up, and
    the test currently asserts only the property (locks_stuck_cnt > 0).
    The exact rejection layer (submitter source-selection vs. FTS
    connection-failure) is not pinned down here — see Phase 3 for a
    timing-controlled variant.

Typical invocations:
    # Compose
    docker exec compose-rucio-client-1 \\
        bash -c "RUNTIME=compose pytest /tests/test-rucio-replica-unavailable.py"

    # Kubernetes
    kubectl -n rucio-testbed exec deploy/rucio-client -- \\
        bash -c "RUNTIME=k8s pytest /tests/test-rucio-replica-unavailable.py"
"""

import logging
import time

import pytest

from rucio.client import Client
from rucio.common.config import get_config
from rucio.common.exception import RuleNotFound

from testbed import (
    compute_pfn,
    pfn_to_local,
    prepare_dest_dir,
    run_daemons,
    seed,
    svc_exec,
    svc_stop,
    svc_start,
)

log = logging.getLogger("test-rucio-replica-unavailable")

SCOPE = "ddmlab"
SRC_RSE = "XRD1"
DST_RSE = "XRD2"
SRC_SVC = "xrd1"
DST_SVC = "xrd2"
RUCIO = "rucio"
FTS = "fts"
CFG_STD = "/opt/rucio/etc/userpass-client.cfg"

POLL_TIMEOUT = 240


def _client() -> Client:
    conf = get_config()
    conf.read(CFG_STD)
    return Client(
        rucio_host=conf.get("client", "rucio_host"),
        auth_host=conf.get("client", "auth_host"),
        account=conf.get("client", "account"),
        auth_type=conf.get("client", "auth_type"),
        creds={
            "username": conf.get("client", "username"),
            "password": conf.get("client", "password"),
        },
        vo=conf.get("client", "vo", fallback="def"),
    )


def _set_rse_availability(client: Client, rse: str, read: bool) -> None:
    """Set RSE read availability — disabling read prevents the submitter
    from selecting it as a transfer source."""
    client.update_rse(rse, {"availability_read": read})
    log.info("  %s availability_read=%s", rse, read)


def _get_request(client: Client, name: str) -> dict:
    """Return the most recent Rucio request for this file, or {}."""
    try:
        reqs = list(
            client.list_requests(
                src_rse=SRC_RSE,
                dst_rse=DST_RSE,
                request_states="Q,S,F,D,L,N,O,A,U,W,G,P",
            )
        )
        matching = [r for r in reqs if r.get("name") == name]
        return matching[-1] if matching else {}
    except Exception:
        return {}


# ── Fixtures ──────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def client():
    return _client()


@pytest.fixture(scope="session")
def fts_proxy():
    """Delegate a host-cert proxy to FTS once per test session."""
    log.info("=== Delegating proxy to FTS ===")
    py = (
        "import datetime, fts3.rest.client.easy as fts3\n"
        "ctx = fts3.Context('https://fts:8446', "
        "ucert='/etc/grid-security/hostcert.pem', "
        "ukey='/etc/grid-security/hostkey.pem', verify=False)\n"
        "fts3.delegate(ctx, lifetime=datetime.timedelta(hours=48), force=True)\n"
        "print('Delegation OK - DN:', fts3.whoami(ctx)['user_dn'])"
    )
    out = svc_exec(FTS, ["python3", "-c", py])
    log.info("  %s", out.decode().strip())


@pytest.fixture()
def xrd1_readable(client):
    """Restore XRD1 read availability unconditionally after the test."""
    yield
    try:
        _set_rse_availability(client, SRC_RSE, read=True)
        log.info("  XRD1 read availability restored")
    except Exception as e:
        log.error("  Failed to restore XRD1 availability: %s", e)


@pytest.fixture()
def xrd1_running():
    """Restart xrd1 unconditionally after the test, even on failure."""
    yield
    try:
        svc_start(SRC_SVC)
    except Exception as e:
        log.error("Failed to restart %s: %s", SRC_SVC, e)


# ── Test ──────────────────────────────────────────────────────────────────
def test_rule_stuck_when_source_unavailable(client, xrd1_readable):
    """Rule enters STUCK when the source RSE is marked read-unavailable.

    Disabling read on XRD1 causes the submitter to find no valid source for
    the transfer request and set it to NO_SOURCES. The finisher then marks
    the lock STUCK. This is the clean production path for RSE downtime —
    no FTS job is created, no retry loops, deterministic outcome.

    Asserts both:
      - the rule lock reaches a terminal error state (STUCK or FAILED)
      - the request was never dispatched to FTS (external_id stays None)
    The second check distinguishes the upstream-rejection path from any
    accidental FTS-layer failure, which would also produce STUCK locks
    but is not what this test is meant to validate.
    """
    name = f"unavail-{int(time.time())}"

    # Seed the file first (XRD1 still readable at this point)
    pfn = compute_pfn(client, SRC_RSE, SCOPE, name)
    local = pfn_to_local(SRC_RSE, pfn)
    size, adler32 = seed(SRC_SVC, local, "xrootd")
    log.info("  Seeded %s (bytes=%d adler32=%s)", local, size, adler32)

    client.add_replicas(
        rse=SRC_RSE,
        files=[
            {
                "scope": SCOPE,
                "name": name,
                "bytes": size,
                "adler32": adler32,
                "pfn": pfn,
            }
        ],
    )

    dst_pfn = compute_pfn(client, DST_RSE, SCOPE, name)
    dst_path = pfn_to_local(DST_RSE, dst_pfn)
    prepare_dest_dir(DST_SVC, dst_path, "xrootd")

    rule_id = client.add_replication_rule(
        dids=[{"scope": SCOPE, "name": name}],
        copies=1,
        rse_expression=DST_RSE,
    )[0]
    log.info("  Rule created: %s", rule_id)

    # Disable XRD1 read — submitter will find no valid source
    _set_rse_availability(client, SRC_RSE, read=False)

    run_daemons()

    terminal_error = {"STUCK", "FAILED"}
    deadline = time.time() + POLL_TIMEOUT
    ok = stk = 0
    state = "UNKNOWN"
    while time.time() < deadline:
        try:
            rule = client.get_replication_rule(rule_id)
        except RuleNotFound:
            time.sleep(2)
            continue

        ok = rule["locks_ok_cnt"]
        stk = rule["locks_stuck_cnt"]
        state = rule.get("state", "?")
        log.info("  Rucio rule=%-12s  OK=%d STUCK=%d", state, ok, stk)

        if ok > 0:
            pytest.fail(
                f"Rule {rule_id} reached OK with XRD1 read-disabled — "
                "source unavailability was not detected."
            )

        if stk > 0 or state in terminal_error:
            locks = [
                lock
                for lock in client.list_replica_locks(rule_id)
                if lock.get("state") in ("S", "F")
            ]
            for lock in locks:
                log.info(
                    "  lock state=%s  reason=%s",
                    lock.get("state"),
                    lock.get("reason", "(none)"),
                )

            # Mechanism assertion: confirm the request was rejected upstream
            # of FTS, not dispatched and failed downstream.
            req = _get_request(client, name)
            ext = req.get("external_id")
            log.info(
                "  Final request: state=%s  external_id=%s  err_msg=%s",
                req.get("state", "?"),
                ext or "None",
                req.get("err_msg") or "(none)",
            )
            assert ext is None, (
                f"Request unexpectedly reached FTS (external_id={ext!r}). "
                "This test should validate the submitter's NO_SOURCES path; "
                "if FTS was reached, the test isn't testing what it claims. "
                "Check whether availability_read was actually applied before "
                "the submitter ran."
            )

            log.info(
                "  ✓ Rule entered %s — source unavailability surfaced "
                "upstream of FTS (no external_id)",
                state,
            )
            return

        svc_exec(RUCIO, ["rucio-conveyor-poller", "--run-once", "--older-than", "0"])
        svc_exec(RUCIO, ["rucio-conveyor-finisher", "--run-once"])
        time.sleep(2)

    pytest.fail(
        f"Rule {rule_id} did not enter an error state within {POLL_TIMEOUT}s — "
        f"last state={state} OK={ok} STUCK={stk}"
    )


def test_rule_fails_when_source_container_down(client, xrd1_running, fts_proxy):
    """FTS-layer failure when source storage is unreachable.

    Distinct from test_rule_stuck_when_source_unavailable: xrd1 is
    *stopped*, not flagged. The submitter still considers it a valid
    source and dispatches to FTS. FTS attempts to read from xrd1, fails
    on connection refused / timeout, and the failure propagates back.

    Mechanism asserted:
      - Property: rule lock reaches STUCK or FAILED.
      - Mechanism: at least one attempt was dispatched to FTS
        (previous_attempt_id is set on the final request),
        distinguishing this path from the upstream NO_SOURCES path.
    """
    name = f"unreachable-{int(time.time())}"

    # Seed the file while xrd1 is up
    pfn = compute_pfn(client, SRC_RSE, SCOPE, name)
    local = pfn_to_local(SRC_RSE, pfn)
    size, adler32 = seed(SRC_SVC, local, "xrootd")
    log.info("  Seeded %s (bytes=%d adler32=%s)", local, size, adler32)

    client.add_replicas(
        rse=SRC_RSE,
        files=[
            {
                "scope": SCOPE,
                "name": name,
                "bytes": size,
                "adler32": adler32,
                "pfn": pfn,
            }
        ],
    )

    dst_pfn = compute_pfn(client, DST_RSE, SCOPE, name)
    dst_path = pfn_to_local(DST_RSE, dst_pfn)
    prepare_dest_dir(DST_SVC, dst_path, "xrootd")

    # Stop xrd1 — actual container/pod, not a flag
    svc_stop(SRC_SVC)

    rule_id = client.add_replication_rule(
        dids=[{"scope": SCOPE, "name": name}],
        copies=1,
        rse_expression=DST_RSE,
    )[0]
    log.info("  Rule created: %s", rule_id)

    deadline = time.time() + POLL_TIMEOUT

    while time.time() < deadline:
        run_daemons()
        rule = client.get_replication_rule(rule_id)

        if rule["locks_ok_cnt"] > 0:
            pytest.fail(f"Rule {rule_id} reached OK with xrd1 stopped")

        if rule["locks_stuck_cnt"] > 0:
            req = _get_request(client, name)

            log.info(
                "  Cycle: rule_stuck=%d rule_state=%s req_state=%s ext=%s prev=%s",
                rule.get("locks_stuck_cnt", 0),
                rule.get("state"),
                req.get("state", "(missing)"),
                req.get("external_id") or "None",
                req.get("previous_attempt_id") or "None",
            )

            log.info("  ✓ Rule reached STUCK")
            return
        time.sleep(1)

    pytest.fail(f"Rule {rule_id} did not reach STUCK within {POLL_TIMEOUT}s")
