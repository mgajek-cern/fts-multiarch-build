"""
testbed.py — shared helpers for the rucio-storage-testbed pytest suites.
"""

import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import zlib

import requests
import urllib3
from typing import Optional

try:
    from rucio.client import Client
    from rucio.rse import rsemanager as rsemgr
except ImportError:
    Client = None
    rsemgr = None

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("testbed")

# ── Runtime ───────────────────────────────────────────────────────────────
RUNTIME = os.environ.get("RUNTIME", "compose")
K8S_NAMESPACE = os.environ.get("K8S_NAMESPACE", "rucio-testbed")

# Maps logical service name → (k8s kind, container name or None)
# container=None means no -c flag (single-container pods / StatefulSets)
K8S_TARGETS: dict[str, tuple[str, Optional[str]]] = {
    "rucio": ("deploy", "rucio"),
    "rucio-oidc": ("deploy", "rucio-oidc"),
    "fts": ("deploy", None),
    "fts-oidc": ("deploy", None),
    "xrd1": ("deploy", None),
    "xrd2": ("deploy", None),
    "xrd3": ("deploy", None),
    "xrd4": ("deploy", None),
    "storm1": ("statefulset", None),
    "storm2": ("statefulset", None),
    "minio1": ("statefulset", None),
    "minio2": ("statefulset", None),
    "webdav1": ("deploy", None),
    "webdav2": ("deploy", None),
}

# curl exit 18 = CURLE_PARTIAL_FILE: FTS Apache/mod_wsgi closes the connection
# after writing a complete response body before curl finishes reading the
# declared Content-Length.  The HTTP status code in stdout is correct; treat
# exit 18 as success everywhere curl talks to FTS.
CURL_OK_EXITS: set[int] = {0, 18}

RUCIO = "rucio"


# ── Subprocess ────────────────────────────────────────────────────────────
def _run(
    cmd: list,
    env: dict = None,
    ok_exits: set = None,
) -> subprocess.CompletedProcess:
    """Run a command locally (inside whatever container pytest is running in).

    Args:
        cmd:      Command and arguments.
        env:      Extra environment variables merged on top of os.environ.
        ok_exits: Set of acceptable exit codes (default: {0}).
    """
    merged = {**os.environ, **(env or {})}
    allowed = ok_exits if ok_exits is not None else {0}
    result = subprocess.run(cmd, capture_output=True, env=merged)
    if result.returncode not in allowed:
        raise RuntimeError(
            f"Command failed (exit {result.returncode}): {' '.join(str(a) for a in cmd)}\n"
            f"stdout: {result.stdout.decode(errors='replace')}\n"
            f"stderr: {result.stderr.decode(errors='replace')}"
        )
    return result


# ── Container exec ────────────────────────────────────────────────────────
def svc_exec(svc: str, cmd: list, user: str = None) -> bytes:
    """Run a command inside a service container (compose or k8s).

    In compose: docker exec [--user USER] compose-<svc>-1 <cmd>
    In k8s:     kubectl -n <ns> exec [kind/]<svc> [-c <container>] -- <cmd>
    """
    if RUNTIME == "compose":
        full = ["docker", "exec"]
        if user:
            full += ["--user", user]
        full += [f"compose-{svc}-1"] + cmd
    elif RUNTIME == "k8s":
        kind, container = K8S_TARGETS.get(svc, ("deploy", None))
        target = f"{kind}/{svc}"
        full = ["kubectl", "-n", K8S_NAMESPACE, "exec"]
        if container:
            full += ["-c", container]
        full += [target, "--"] + cmd
    else:
        raise RuntimeError(f"Unknown RUNTIME: {RUNTIME!r}")

    result = subprocess.run(full, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"svc_exec failed (exit {result.returncode}): {' '.join(full)}\n"
            f"stdout: {result.stdout.decode(errors='replace')}\n"
            f"stderr: {result.stderr.decode(errors='replace')}"
        )
    return result.stdout


def _xrd_service_manifest(svc: str, port: int = 1094) -> str:
    """Minimal Service manifest matching what the Helm chart renders.

    Used by svc_start to recreate Services that svc_stop deletes.
    Restoring a freshly-rendered minimal manifest is more robust than
    round-tripping `kubectl get svc -o yaml`, which contains many
    runtime-assigned fields that need stripping.
    """
    return f"""\
apiVersion: v1
kind: Service
metadata:
  name: {svc}
  namespace: {K8S_NAMESPACE}
  labels:
    app: xrootd
    instance: {svc}
spec:
  type: ClusterIP
  ports:
    - port: {port}
      targetPort: xrootd
      protocol: TCP
      name: xrootd
  selector:
    app: xrootd
    instance: {svc}
"""


def svc_stop(svc: str) -> None:
    """Stop a service container/pod (runtime-agnostic).

    compose: docker stop compose-<svc>-1
    k8s:     scale to 0 (waits for pod gone) and DELETE the Service so
             DNS returns NXDOMAIN. Without the Service delete, FTS sees
             ECONNREFUSED from kube-proxy and the xrootd client retries
             internally for ~6 min per attempt.
    """
    if RUNTIME == "compose":
        _run(["docker", "stop", f"compose-{svc}-1"])
    elif RUNTIME == "k8s":
        kind, _ = K8S_TARGETS.get(svc, ("deploy", None))
        _run(["kubectl", "-n", K8S_NAMESPACE, "scale", f"{kind}/{svc}", "--replicas=0"])
        _wait_for_pod_deleted(svc)
        # Best-effort delete; ignore if Service is already gone or RBAC denies.
        subprocess.run(
            [
                "kubectl",
                "-n",
                K8S_NAMESPACE,
                "delete",
                "svc",
                svc,
                "--ignore-not-found=true",
            ],
            capture_output=True,
            check=False,
        )
        log.info("  ✓ Deleted Service %s", svc)
    else:
        raise RuntimeError(f"Unknown RUNTIME: {RUNTIME!r}")
    log.info("  ✓ Stopped %s", svc)


def svc_start(svc: str) -> None:
    """Start a previously-stopped service (runtime-agnostic).

    compose: docker start
    k8s:     re-create the Service from a minimal hardcoded manifest,
             then scale deployment to 1 and wait until ready.
    """
    if RUNTIME == "compose":
        _run(["docker", "start", f"compose-{svc}-1"])
    elif RUNTIME == "k8s":
        # Re-create Service first; idempotent via `kubectl apply`.
        proc = subprocess.run(
            ["kubectl", "-n", K8S_NAMESPACE, "apply", "-f", "-"],
            input=_xrd_service_manifest(svc).encode(),
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Service {svc} restore failed: {proc.stderr.decode(errors='replace')}"
            )
        kind, _ = K8S_TARGETS.get(svc, ("deploy", None))
        _run(["kubectl", "-n", K8S_NAMESPACE, "scale", f"{kind}/{svc}", "--replicas=1"])
        _wait_for_replicas(svc, expected=1, ready=True)
    else:
        raise RuntimeError(f"Unknown RUNTIME: {RUNTIME!r}")
    log.info("  ✓ Started %s", svc)


def _wait_for_replicas(
    svc: str,
    expected: int,
    ready: bool = False,
    retries: int = 30,
    interval: int = 2,
) -> None:
    kind, _ = K8S_TARGETS.get(svc, ("deploy", None))

    # Simple rule: if we want it up, check 'readyReplicas'.
    # If we want it down, 'replicas' (total count) must be 0.
    field = "readyReplicas" if (ready and expected > 0) else "replicas"

    for _ in range(retries):
        result = _run(
            [
                "kubectl",
                "-n",
                K8S_NAMESPACE,
                "get",
                f"{kind}/{svc}",
                "-o",
                f"jsonpath={{.status.{field}}}",
            ]
        )

        # If the output is empty, it means K8s removed the key (common at 0 replicas)
        output = result.stdout.decode().strip()
        current = int(output) if output else 0

        if current == expected:
            return
        time.sleep(interval)

    raise RuntimeError(f"{svc} failed to reach {expected} replicas")


def _wait_for_pod_deleted(svc: str, retries: int = 60, interval: int = 2) -> None:
    """Wait until no pods matching the deployment's selector remain.

    Required because kubectl scale --replicas=0 returns once the spec is
    updated, but the pod can stay in 'Terminating' phase serving traffic
    for up to terminationGracePeriodSeconds. .status.replicas going to 0
    only means 'no desired replicas', not 'no actual pod'.
    """
    for _ in range(retries):
        result = _run(
            [
                "kubectl",
                "-n",
                K8S_NAMESPACE,
                "get",
                "pods",
                "-l",
                f"instance={svc}",
                "-o",
                "jsonpath={.items[*].metadata.name}",
            ]
        )
        if not result.stdout.decode().strip():
            return
        time.sleep(interval)
    raise RuntimeError(f"{svc} pods still present after {retries * interval}s")


# ── XRootD helpers ────────────────────────────────────────────────────────
def xrd_seed(xrd_url: str, content: str, proxy: str = None) -> None:
    """Write content to an XRootD URL via xrdcp."""
    proxy = proxy or os.environ.get("X509_USER_PROXY", "/tmp/x509up_u0")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".seed", delete=False) as f:
        f.write(content)
        tmp = f.name
    try:
        _run(["xrdcp", "--force", tmp, xrd_url], env={"X509_USER_PROXY": proxy})
        log.info("  ✓ Seeded %s via xrdcp", xrd_url)
    finally:
        os.unlink(tmp)


def xrd_exists(xrd_url: str, proxy: str = None) -> bool:
    """Return True if the XRootD path exists (xrdfs stat)."""
    proxy = proxy or os.environ.get("X509_USER_PROXY", "/tmp/x509up_u0")
    without_scheme = xrd_url.replace("root://", "")
    host, _, path = without_scheme.partition("/")
    path = "/" + path.lstrip("/")
    try:
        _run(["xrdfs", host, "stat", path], env={"X509_USER_PROXY": proxy})
        return True
    except RuntimeError:
        return False


def xrd_read(xrd_url: str, proxy: str = None) -> str:
    """Read an XRootD file via xrdcp and return its content as a string."""
    proxy = proxy or os.environ.get("X509_USER_PROXY", "/tmp/x509up_u0")
    with tempfile.NamedTemporaryFile(suffix=".read", delete=False) as f:
        tmp = f.name
    try:
        _run(["xrdcp", "--force", xrd_url, tmp], env={"X509_USER_PROXY": proxy})
        with open(tmp) as f:
            return f.read()
    finally:
        os.unlink(tmp)


# ── gfal helpers (SciTokens / OIDC) ──────────────────────────────────────
def davs_seed(url: str, content: str, token: str) -> None:
    """Write content to a davs:// URL via gfal-copy with a bearer token."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".seed", delete=False) as f:
        f.write(content)
        tmp = f.name
    try:
        _run(
            ["gfal-copy", "--force", f"file://{tmp}", url],
            env={"BEARER_TOKEN": token},
        )
        log.info("  ✓ Seeded %s via gfal-copy", url)
    finally:
        os.unlink(tmp)


def davs_exists(url: str, token: str) -> bool:
    """Return True if the davs:// path exists (gfal-stat)."""
    try:
        _run(["gfal-stat", url], env={"BEARER_TOKEN": token})
        return True
    except RuntimeError:
        return False


def davs_read(url: str, token: str) -> str:
    """Read a davs:// file via gfal-copy and return its content as a string."""
    with tempfile.NamedTemporaryFile(suffix=".read", delete=False) as f:
        tmp = f.name
    try:
        _run(
            ["gfal-copy", "--force", url, f"file://{tmp}"],
            env={"BEARER_TOKEN": token},
        )
        with open(tmp) as f:
            return f.read()
    finally:
        os.unlink(tmp)


# ── FTS curl helpers (GSI cert auth) ─────────────────────────────────────
def fts_curl(
    *args: str,
    cert: str = None,
    key: str = None,
    cacert: str = None,
) -> str:
    """Run curl against the FTS REST API with client cert auth.

    Tolerates curl exit 18 (CURLE_PARTIAL_FILE) — FTS Apache/mod_wsgi
    closes the connection after the response body, before curl finishes
    reading the declared Content-Length.  The response is complete.
    """
    cert = cert or os.environ.get("CERT", "/etc/grid-security/hostcert.pem")
    key = key or os.environ.get("KEY", "/etc/grid-security/hostkey.pem")
    cacert = cacert or os.environ.get(
        "CACERT", "/etc/grid-security/certificates/rucio_ca.pem"
    )
    cmd = ["curl", "-sk", "--cert", cert, "--key", key, "--cacert", cacert] + list(args)
    result = _run(cmd, ok_exits=CURL_OK_EXITS)
    return result.stdout.decode(errors="replace")


def fts_curl_code(*args: str, **kwargs) -> str:
    """Run curl against FTS and return only the HTTP status code string.

    Uses -D - (dump headers to stdout) to parse the status line rather
    than -o /dev/null -w %{http_code}, which hits the partial-read issue.
    """
    body = fts_curl("-D", "-", *args, **kwargs)
    for line in body.splitlines():
        if line.startswith("HTTP/"):
            return line.split()[1]
    return "000"


# ── FTS job polling ───────────────────────────────────────────────────────
def poll_fts_job(
    ctx,
    job_id: str,
    retries: int = 24,
    interval: int = 5,
) -> str:
    """Poll an FTS job (via fts3 Python client) until terminal state.

    Returns the final job_state string.
    Logs file-level error details on non-FINISHED terminal states.
    """
    try:
        import fts3.rest.client.easy as fts3
    except ImportError:
        raise RuntimeError("fts3 module not available")

    terminal = {"FINISHED", "FAILED", "CANCELED", "FINISHEDDIRTY"}
    state = "UNKNOWN"
    for i in range(1, retries + 1):
        time.sleep(interval)
        status = fts3.get_job_status(ctx, job_id, list_files=False)
        state = status["job_state"]
        log.info("  [%3ds] %s", i * interval, state)
        if state in terminal:
            break

    log.info("Final state: %s", state)

    if state != "FINISHED":
        try:
            files = fts3.get_job_status(ctx, job_id, list_files=True)
            for f in files.get("files", []):
                log.error("  %s: %s", f["file_state"], f.get("reason", ""))
        except Exception:
            pass

    return state


def poll_fts_job_http(
    fts_url: str,
    job_id: str,
    token: str,
    retries: int = 30,
    interval: int = 5,
) -> str:
    """Poll an FTS job via plain HTTP (bearer token auth).

    Used by OIDC tests that talk to fts-oidc without the fts3 Python client.
    Returns the final job_state string.
    """
    terminal = {"FINISHED", "FAILED", "CANCELED", "FINISHEDDIRTY"}
    state = "UNKNOWN"
    for i in range(1, retries + 1):
        time.sleep(interval)
        resp = requests.get(
            f"{fts_url}/jobs/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
            verify=False,
            timeout=10,
        )
        resp.raise_for_status()
        state = resp.json()["job_state"]
        log.info("  [%3ds] %s", i * interval, state)
        if state in terminal:
            break

    if state != "FINISHED":
        try:
            files_resp = requests.get(
                f"{fts_url}/jobs/{job_id}/files",
                headers={"Authorization": f"Bearer {token}"},
                verify=False,
                timeout=10,
            )
            for f in files_resp.json():
                log.error("  %s: %s", f.get("file_state"), f.get("reason", ""))
        except Exception:
            pass

    return state


# ── Keycloak token helpers ────────────────────────────────────────────────
def fetch_token_client_credentials(
    keycloak_url: str,
    client_id: str,
    client_secret: str,
    scope: str,
    audience: str,
) -> str:
    """Fetch a client-credentials token from Keycloak."""
    resp = requests.post(
        keycloak_url,
        data={
            "grant_type": "client_credentials",
            "scope": scope,
            "audience": audience,
        },
        auth=(client_id, client_secret),
        verify=False,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_token_password(
    keycloak_url: str,
    client_id: str,
    client_secret: str,
    username: str,
    password: str,
    scope: str = "openid",
) -> str:
    """Fetch a resource-owner password token from Keycloak."""
    resp = requests.post(
        keycloak_url,
        data={
            "grant_type": "password",
            "username": username,
            "password": password,
            "scope": scope,
        },
        auth=(client_id, client_secret),
        verify=False,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def decode_jwt_claims(token: str) -> dict:
    """Decode a JWT payload without signature verification (logging only)."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def compute_pfn(client: "Client", rse: str, scope: str, name: str) -> str:
    """Compute PFN via the Rucio RSE manager.

    Requires the optional rucio-clients package.
    """
    if Client is None or rsemgr is None:
        raise RuntimeError("compute_pfn requires the rucio-clients package")

    rse_info = rsemgr.get_rse_info(rse=rse, vo=client.vo)

    return list(
        rsemgr.lfns2pfns(
            rse_info,
            [{"scope": scope, "name": name}],
            operation="write",
        ).values()
    )[0]


# ── Rucio transfer helpers ────────────────────────────────────────────────


def prepare_dest_dir(storage_svc: str, fpath: str, owner: str) -> None:
    script = (
        f'mkdir -p "$(dirname {fpath})" && '
        f'chown {owner}:{owner} "$(dirname {fpath})" 2>/dev/null || true'
    )
    svc_exec(storage_svc, ["sh", "-c", script], user="root")
    log.info("  ✓ Destination dir ready on %s: %s", storage_svc, fpath)


def pfn_to_local(rse: str, pfn: str) -> str:
    """Convert a PFN URL into the path inside the storage container.

    StoRM RSEs use a different mount layout than the generic XRootD/WebDAV
    case; the storm-specific regex strips the protocol+host+port and
    rewrites /data/ to /storage/data/.
    """
    if rse.startswith("STORM"):
        return re.sub(r"^[a-z]+://storm[1-2]:[0-9]+/data/", "/storage/data/", pfn)
    return re.sub(r"^/+", "/", re.sub(r"^[a-z]+://[^/]+", "", pfn))


def seed(svc: str, fpath: str, owner: str) -> tuple:
    script = (
        "set -e; "
        f'mkdir -p "$(dirname {fpath})"; '
        f'printf "rucio-test\\n" > {fpath}; '
        f"chown {owner}:{owner} {fpath} 2>/dev/null || true"
    )
    svc_exec(svc, ["sh", "-c", script], user="root")
    raw = svc_exec(svc, ["cat", fpath])
    return len(raw), "%08x" % (zlib.adler32(raw) & 0xFFFFFFFF)


def run_daemons(svc: str = RUCIO) -> None:
    for daemon in (
        ["rucio-judge-evaluator", "--run-once"],
        ["rucio-conveyor-submitter", "--run-once"],
        ["rucio-conveyor-poller", "--run-once", "--older-than", "0"],
        ["rucio-conveyor-finisher", "--run-once"],
    ):
        log.info("  → %s %s", svc, " ".join(daemon))
        output = svc_exec(svc, daemon)

        decoded_output = (output or b"").decode("utf-8", errors="replace")
        for line in decoded_output.splitlines():
            if any(
                k in line.lower()
                for k in ("warning", "error", "checksum", "failed", "submit")
            ):
                log.info("    | %s", line)


# ── Health / readiness polling ────────────────────────────────────────────
def wait_for_http(
    url: str,
    label: str,
    expected_codes: set = None,
    retries: int = 30,
    interval: int = 5,
    method: str = "GET",
    headers: dict = None,
) -> None:
    """Poll an HTTP endpoint until it returns one of the expected status codes."""
    expected_codes = expected_codes or {200}
    for i in range(1, retries + 1):
        try:
            resp = requests.request(
                method,
                url,
                headers=headers or {},
                verify=False,
                timeout=5,
            )
            if resp.status_code in expected_codes:
                log.info("  ✓ %s ready (HTTP %s)", label, resp.status_code)
                return
            log.info(
                "  [%d] %s not ready (HTTP %s) — waiting", i, label, resp.status_code
            )
        except requests.RequestException as e:
            log.info("  [%d] %s unreachable (%s) — waiting", i, label, e)
        time.sleep(interval)
    raise RuntimeError(f"{label} did not become ready after {retries} attempts")
