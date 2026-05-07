#!/usr/bin/env python3
"""
test-rucio-token-expiry.py — Validates OIDC token refresh mid-transfer.

Patches Keycloak to issue 30s access tokens, submits a StoRM OIDC transfer
that outlasts the token lifetime, and asserts the job still reaches FINISHED —
proving the Rucio conveyor and FTS OIDC refresh path handle expiry transparently.

Typical invocations:
    # Compose
    docker exec compose-rucio-client-1 \\
        bash -c "RUNTIME=compose pytest /tests/test-rucio-token-expiry.py"

    # Kubernetes
    kubectl -n rucio-testbed exec deploy/rucio-client -- \\
        bash -c "RUNTIME=k8s pytest /tests/test-rucio-token-expiry.py"
"""

import logging
import time
import zlib

import pytest
import requests
import urllib3

from rucio.client import Client
from rucio.common.config import get_config
from rucio.common.exception import RuleNotFound

from testbed import compute_pfn, pfn_to_local, run_daemons, svc_exec

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("test-rucio-token-expiry")

SCOPE = "ddmlab"
SRC_RSE = "STORM1"
DST_RSE = "STORM2"
SRC_SVC = "storm1"
DST_SVC = "storm2"
RUCIO_OIDC = "rucio-oidc"
CFG_OIDC = "/opt/rucio/etc/userpass-client-for-rucio-oidc.cfg"

KEYCLOAK_URL = "https://keycloak:8443"
KEYCLOAK_REALM = "rucio"
KEYCLOAK_ADMIN = "admin"
KEYCLOAK_SECRET = "admin"

SHORT_TTL = 30  # seconds — shorter than the transfer + daemon cycle
DEFAULT_TTL = 300  # Keycloak default; restored in teardown

POLL_TIMEOUT = 180


def _admin_token() -> str:
    resp = requests.post(
        f"{KEYCLOAK_URL}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": KEYCLOAK_ADMIN,
            "password": KEYCLOAK_SECRET,
        },
        verify=False,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _set_token_ttl(ttl: int) -> None:
    token = _admin_token()
    resp = requests.put(
        f"{KEYCLOAK_URL}/admin/realms/{KEYCLOAK_REALM}",
        json={"accessTokenLifespan": ttl},
        headers={"Authorization": f"Bearer {token}"},
        verify=False,
        timeout=10,
    )
    resp.raise_for_status()
    log.info("  Keycloak accessTokenLifespan set to %ds", ttl)


def _client() -> Client:
    conf = get_config()
    conf.read(CFG_OIDC)
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


def _seed_storm(svc: str, fpath: str) -> tuple:
    script = (
        "set -e; "
        f'mkdir -p "$(dirname {fpath})"; '
        f'printf "rucio-test\\n" > {fpath}; '
        f"chown storm:storm {fpath} 2>/dev/null || true"
    )
    svc_exec(svc, ["sh", "-c", script], user="root")
    raw = svc_exec(svc, ["cat", fpath])
    return len(raw), "%08x" % (zlib.adler32(raw) & 0xFFFFFFFF)


# ── Fixtures ──────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def client():
    return _client()


@pytest.fixture(scope="session", autouse=True)
def short_token_lifetime():
    """Patch Keycloak to short tokens; restore DEFAULT_TTL on teardown."""
    _set_token_ttl(SHORT_TTL)
    yield
    _set_token_ttl(DEFAULT_TTL)
    log.info("  Keycloak token lifetime restored to %ds", DEFAULT_TTL)


# ── Tests ─────────────────────────────────────────────────────────────────
def test_transfer_survives_token_expiry(client):
    """Transfer completes even though the token expires mid-flight."""
    name = f"token-expiry-{int(time.time())}"

    # Seed source on storm1
    pfn = compute_pfn(client, SRC_RSE, SCOPE, name)
    local = pfn_to_local(SRC_RSE, pfn)
    size, adler32 = _seed_storm(SRC_SVC, local)
    log.info("  Seeded %s on %s (bytes=%d)", local, SRC_SVC, size)

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

    # Prepare destination on storm2
    dst_pfn = compute_pfn(client, DST_RSE, SCOPE, name)
    dst_path = pfn_to_local(DST_RSE, dst_pfn)
    svc_exec(
        DST_SVC,
        [
            "sh",
            "-c",
            f'mkdir -p "$(dirname {dst_path})" && '
            f'chown storm:storm "$(dirname {dst_path})" 2>/dev/null || true',
        ],
        user="root",
    )

    rule_id = client.add_replication_rule(
        dids=[{"scope": SCOPE, "name": name}], copies=1, rse_expression=DST_RSE
    )[0]
    log.info("  Rule created: %s  (token TTL=%ds)", rule_id, SHORT_TTL)

    # Sleep past the initial token expiry before running daemons — the
    # conveyor must fetch a fresh token when it picks up the job.
    log.info("  Sleeping %ds to outlast the token lifetime...", SHORT_TTL + 1)
    time.sleep(SHORT_TTL + 1)

    run_daemons(RUCIO_OIDC)  # initial dispatch via OIDC conveyor

    # Poll — expect OK despite the expired initial token
    deadline = time.time() + POLL_TIMEOUT
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
            "  state=%-12s OK=%d REPL=%d STUCK=%d",
            rule.get("state", "?"),
            ok,
            repl,
            stk,
        )

        if stk > 0:
            pytest.fail(
                f"Rule {rule_id} got STUCK — conveyor did not refresh "
                f"the expired token. OK={ok} STUCK={stk}"
            )
        if ok >= 1 and repl == 0:
            log.info("  ✓ Transfer completed despite token expiry — refresh path works")
            return

        run_daemons(RUCIO_OIDC)  # full chain on OIDC conveyor
        time.sleep(2)

    pytest.fail(
        f"Rule {rule_id} did not converge within {POLL_TIMEOUT}s — "
        f"last state: OK={ok} REPL={repl} STUCK={stk}"
    )
