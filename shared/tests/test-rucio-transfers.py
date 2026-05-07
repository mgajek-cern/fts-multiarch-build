#!/usr/bin/env python3
"""
test-rucio-transfers.py — Rucio E2E transfer tests using the Python client.

Covers three scenarios:
  - XRootD GSI   (XRD1  → XRD2,   rucio    instance, X.509 cert auth)
  - StoRM OIDC   (STORM1 → STORM2, rucio-oidc instance, bearer tokens)
  - XRootD OIDC  (XRD3  → XRD4,   rucio-oidc instance, SciTokens)

Runtime-agnostic: respects $RUNTIME (compose | k8s, default compose).

Typical invocations:
    # Compose
    docker exec compose-rucio-client-1 \\
        bash -c "RUNTIME=compose pytest /tests/test-rucio-transfers.py"

    # Kubernetes
    kubectl -n rucio-testbed exec deploy/rucio-client -- \\
        bash -c "RUNTIME=k8s pytest /tests/test-rucio-transfers.py"
"""

import logging
import time

import pytest

from rucio.client import Client
from rucio.common.config import get_config
from rucio.common.exception import Duplicate, RucioException, RuleNotFound

from testbed import (
    RUNTIME,
    compute_pfn,
    pfn_to_local,
    prepare_dest_dir,
    run_daemons,
    seed,
    svc_exec,
)


log = logging.getLogger("rucio-transfers")

# ── Topology ───────────────────────────────────────────────────────────────
RUCIO = "rucio"
RUCIO_OIDC = "rucio-oidc"
XRD1, XRD2, XRD3, XRD4 = "xrd1", "xrd2", "xrd3", "xrd4"
STORM1, STORM2 = "storm1", "storm2"
FTS = "fts"

CFG_STD = "/opt/rucio/etc/userpass-client.cfg"
CFG_OIDC = "/opt/rucio/etc/userpass-client-for-rucio-oidc.cfg"


# ── Rucio client factory ───────────────────────────────────────────────────
def _client(cfg_path: str) -> Client:
    conf = get_config()
    conf.read(cfg_path)
    rucio_host = conf.get("client", "rucio_host")
    c = Client(
        rucio_host=rucio_host,
        auth_host=conf.get("client", "auth_host"),
        account=conf.get("client", "account"),
        auth_type=conf.get("client", "auth_type"),
        creds={
            "username": conf.get("client", "username"),
            "password": conf.get("client", "password"),
        },
        vo=conf.get("client", "vo", fallback="def"),
    )
    log.info(
        "Connected to Rucio at %s (VO: %s, runtime: %s)", rucio_host, c.vo, RUNTIME
    )
    return c


# ── Fixtures ───────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def client_std() -> Client:
    return _client(CFG_STD)


@pytest.fixture(scope="session")
def client_oidc() -> Client:
    return _client(CFG_OIDC)


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


# ── Storage helpers ────────────────────────────────────────────────────────


def register_replica(
    client: Client,
    rse: str,
    scope: str,
    name: str,
    pfn: str,
    size: int,
    adler32: str,
) -> None:
    log.info(
        "  Registering %s:%s at %s (bytes=%d adler32=%s)",
        scope,
        name,
        rse,
        size,
        adler32,
    )
    try:
        client.add_replicas(
            rse=rse,
            files=[
                {
                    "scope": scope,
                    "name": name,
                    "bytes": size,
                    "adler32": adler32,
                    "pfn": pfn,
                }
            ],
        )
        log.info("  ✓ Replica registered at %s", rse)
    except Duplicate:
        log.warning("  Replica %s:%s already exists", scope, name)
    except RucioException as e:
        log.error("  Registration failed: %s", e)
        raise


def add_rule(client: Client, scope: str, name: str, dst_rse: str) -> str:
    rule_id = client.add_replication_rule(
        dids=[{"scope": scope, "name": name}], copies=1, rse_expression=dst_rse
    )[0]
    log.info("  ✓ Rule created: %s → %s (%s)", name, dst_rse, rule_id)
    return rule_id


def validate_rule(
    client: Client,
    rule_id: str,
    label: str,
    rucio_svc: str,
    timeout: int = 180,
) -> None:
    """Poll until locks_ok >= 1 and locks_replicating == 0; cycle daemons each loop."""
    log.info("=== Validating rule %s (%s) ===", rule_id, label)
    deadline = time.time() + timeout
    ok = repl = stk = 0
    while time.time() < deadline:
        try:
            rule = client.get_replication_rule(rule_id)
        except RuleNotFound:
            time.sleep(2)
            continue

        ok = rule["locks_ok_cnt"]
        repl = rule["locks_replicating_cnt"]
        stk = rule["locks_stuck_cnt"]
        log.info(
            "  rule state=%-12s  locks OK=%-3d REPL=%-3d STUCK=%-3d  expires=%s",
            rule.get("state", "?"),
            ok,
            repl,
            stk,
            rule.get("expires_at", "never"),
        )

        if stk > 0:
            try:
                for lock in [
                    replica_lock
                    for replica_lock in client.list_replica_locks(rule_id)
                    if replica_lock.get("state") == "S"
                ]:
                    log.error(
                        "  STUCK lock: rse=%-12s  scope=%s  name=%s",
                        lock.get("rse_id"),
                        lock.get("scope"),
                        lock.get("name"),
                    )
            except Exception as e:
                log.warning("  Could not list stuck locks: %s", e)
            raise RuntimeError(
                f"Rule {rule_id} ({label}) has {stk} stuck lock(s) — see STUCK entries above"
            )

        if ok >= 1 and repl == 0:
            log.info("  ✓ %s passed (rule_id=%s)", label, rule_id)
            return

        log.info("  (advancing transfer — running poller + finisher)")
        svc_exec(
            rucio_svc, ["rucio-conveyor-poller", "--run-once", "--older-than", "0"]
        )
        svc_exec(rucio_svc, ["rucio-conveyor-finisher", "--run-once"])
        time.sleep(5)

    raise TimeoutError(
        f"Rule {rule_id} ({label}) did not converge within {timeout}s — "
        f"last state: OK={ok} REPL={repl} STUCK={stk}"
    )


def transfer_workflow(
    client: Client,
    src_rse: str,
    dst_rse: str,
    src_svc: str,
    dst_svc: str,
    scope: str,
    name: str,
    rucio_svc: str,
    owner: str,
    label: str,
) -> None:
    log.info("[ Test: %s (%s → %s) ]", label, src_rse, dst_rse)

    pfn = compute_pfn(client, src_rse, scope, name)
    local = pfn_to_local(src_rse, pfn)
    log.info("  PFN:            %s", pfn)
    log.info("  Container path: %s", local)

    size, adler32 = seed(src_svc, local, owner)
    register_replica(client, src_rse, scope, name, pfn, size, adler32)

    dst_path = pfn_to_local(dst_rse, compute_pfn(client, dst_rse, scope, name))
    prepare_dest_dir(dst_svc, dst_path, owner)

    rule_id = add_rule(client, scope, name, dst_rse)
    run_daemons(rucio_svc)
    validate_rule(client, rule_id, label, rucio_svc)


# ── Tests ──────────────────────────────────────────────────────────────────
def test_xrootd_gsi(client_std, fts_proxy):
    """XRootD GSI TPC: XRD1 → XRD2 via rucio (X.509 cert auth)."""
    transfer_workflow(
        client_std,
        src_rse="XRD1",
        dst_rse="XRD2",
        src_svc=XRD1,
        dst_svc=XRD2,
        scope="ddmlab",
        name=f"gsi-{int(time.time())}",
        rucio_svc=RUCIO,
        owner="xrootd",
        label="XRootD GSI",
    )


def test_storm_oidc(client_oidc):
    """StoRM WebDAV OIDC TPC: STORM1 → STORM2 via rucio-oidc."""
    transfer_workflow(
        client_oidc,
        src_rse="STORM1",
        dst_rse="STORM2",
        src_svc=STORM1,
        dst_svc=STORM2,
        scope="ddmlab",
        name=f"storm-{int(time.time())}",
        rucio_svc=RUCIO_OIDC,
        owner="storm",
        label="StoRM OIDC",
    )


def test_xrootd_oidc(client_oidc):
    """XRootD SciTokens TPC: XRD3 → XRD4 via rucio-oidc."""
    transfer_workflow(
        client_oidc,
        src_rse="XRD3",
        dst_rse="XRD4",
        src_svc=XRD3,
        dst_svc=XRD4,
        scope="ddmlab",
        name=f"xrd-oidc-{int(time.time())}",
        rucio_svc=RUCIO_OIDC,
        owner="xrootd",
        label="XRootD OIDC",
    )


def test_add_dataset(client_std, fts_proxy):
    """Register two files into a new dataset on XRD1, replicate to XRD2.

    Demonstrates add_dataset — the recommended single-call pattern for
    atomically creating a dataset and registering its initial replicas.
    """
    scope = "ddmlab"
    dataset = f"gsi-dataset-{int(time.time())}"
    files = [
        {"name": f"{dataset}-file1", "content": "rucio-test-file1\n"},
        {"name": f"{dataset}-file2", "content": "rucio-test-file2\n"},
    ]

    log.info("[ Test: add_dataset — XRD1 (seed 2 files + dataset) → XRD2 ]")

    registered = []
    for f in files:
        pfn = compute_pfn(client_std, "XRD1", scope, f["name"])
        local = pfn_to_local("XRD1", pfn)
        size, adler32 = seed(XRD1, local, "xrootd")
        registered.append(
            {
                "scope": scope,
                "name": f["name"],
                "bytes": size,
                "adler32": adler32,
                "pfn": pfn,
            }
        )
        log.info("  seeded %s → %s", f["name"], local)

    log.info("  Creating dataset %s:%s with %d files", scope, dataset, len(registered))
    client_std.add_dataset(scope=scope, name=dataset, rse="XRD1", files=registered)
    log.info("  ✓ Dataset registered")

    # Prepare destination dirs for both files
    for f in registered:
        dst_path = pfn_to_local(
            "XRD2", compute_pfn(client_std, "XRD2", scope, f["name"])
        )
        prepare_dest_dir(XRD2, dst_path, "xrootd")

    # Single rule on the dataset DID — Rucio expands it to per-file rules
    rule_id = add_rule(client_std, scope, dataset, "XRD2")
    run_daemons(RUCIO)
    validate_rule(client_std, rule_id, "add_dataset XRD1→XRD2", RUCIO)


def test_add_files_to_dataset(client_std, fts_proxy):
    """Append two new files to an existing dataset on XRD1, replicate to XRD2.

    Demonstrates add_files_to_dataset — the pattern for extending an
    existing dataset with new replicas in a single API call.
    """
    scope = "ddmlab"
    dataset = f"gsi-existing-dataset-{int(time.time())}"

    # Create the dataset first (empty)
    log.info("[ Test: add_files_to_dataset — extend existing dataset → XRD2 ]")
    client_std.add_dataset(scope=scope, name=dataset)
    log.info("  Created empty dataset %s:%s", scope, dataset)

    files = [
        {"name": f"{dataset}-v2-file1", "content": "rucio-append-v2-file1\n"},
        {"name": f"{dataset}-v2-file2", "content": "rucio-append-v2-file2\n"},
    ]

    registered = []
    for f in files:
        pfn = compute_pfn(client_std, "XRD1", scope, f["name"])
        local = pfn_to_local("XRD1", pfn)
        size, adler32 = seed(XRD1, local, "xrootd")
        registered.append(
            {
                "scope": scope,
                "name": f["name"],
                "bytes": size,
                "adler32": adler32,
                "pfn": pfn,
            }
        )
        log.info("  seeded %s → %s", f["name"], local)

    log.info("  Appending %d files to %s:%s", len(registered), scope, dataset)
    client_std.add_files_to_dataset(
        scope=scope, name=dataset, rse="XRD1", files=registered
    )
    log.info("  ✓ Files appended to existing dataset")

    for f in registered:
        dst_path = pfn_to_local(
            "XRD2", compute_pfn(client_std, "XRD2", scope, f["name"])
        )
        prepare_dest_dir(XRD2, dst_path, "xrootd")

    rule_id = add_rule(client_std, scope, dataset, "XRD2")
    run_daemons(RUCIO)
    validate_rule(client_std, rule_id, "add_files_to_dataset XRD1→XRD2", RUCIO)
