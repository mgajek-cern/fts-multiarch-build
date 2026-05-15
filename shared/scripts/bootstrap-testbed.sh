#!/usr/bin/env bash
set -euo pipefail

RUNTIME="${RUNTIME:-compose}"
K8S_NAMESPACE="${K8S_NAMESPACE:-rucio-testbed}"
COMPOSE_FILE="${COMPOSE_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/deploy/compose/docker-compose.yml}"

# ─── Cross-runtime helpers ───────────────────────────────────────

# _exec <service> -- <command…>
#
# In compose mode: docker exec compose-<service>-1 <command…>
# In k8s mode:     kubectl exec deploy/<service> [-c <svc>] -- <command…>
#
# StatefulSet pods are addressed as pod/<name>-0 with no -c flag (single
# container, name varies by chart). Sidecar Deployments (rucio, rucio-oidc)
# need -c to pick the app container, not the log-tailer sidecar.
_exec() {
    local svc=$1; shift
    case "$RUNTIME" in
        compose)
            docker exec "compose-${svc}-1" "$@"
            ;;
        k8s)
            local target
            local -a cflag=()
            case "$svc" in
                ftsdb|ftsdb-oidc|ruciodb|ruciodb-oidc|storm1|storm2|minio1|minio2)
                    target="pod/${svc}-0" ;;
                rucio|rucio-oidc)
                    target="deploy/${svc}"; cflag=(-c "$svc") ;;
                *)
                    target="deploy/${svc}" ;;
            esac
            kubectl -n "$K8S_NAMESPACE" exec "$target" "${cflag[@]}" -- "$@"
            ;;
        *) echo "Unknown RUNTIME: $RUNTIME" >&2; return 2 ;;
    esac
}

# Graceful restart of one or more services.
_restart() {
    case "$RUNTIME" in
        compose)
            docker compose -f "$COMPOSE_FILE" restart "$@" ;;
        k8s)
            for svc in "$@"; do
                local target
                case "$svc" in
                    storm1|storm2|minio1|minio2|ftsdb*|ruciodb*)
                        target="statefulset/${svc}" ;;
                    *)
                        target="deploy/${svc}" ;;
                esac
                kubectl -n "$K8S_NAMESPACE" rollout restart "$target"
                kubectl -n "$K8S_NAMESPACE" rollout status  "$target" --timeout=120s
            done ;;
    esac
}

# Probe an HTTP endpoint as seen "from the outside".
# Compose: hits the host's localhost (relies on published ports).
# K8s:     execs into rucio-oidc and curls localhost there.
# Returns the HTTP status code (or "000" on connection failure).
_http_probe_local() {
    local port=$1 path=$2
    case "$RUNTIME" in
        compose)
            curl -s -o /dev/null -w '%{http_code}' \
                "http://localhost:${port}${path}" || true ;;
        k8s)
            _exec rucio-oidc curl -s -o /dev/null -w '%{http_code}' \
                "http://localhost${path}" 2>/dev/null || true ;;
    esac
}

# ── Service URLs (used by RSE config) ───────────────────────────────────────
FTS="https://fts:8446"
FTS_OIDC="https://fts-oidc:8446"

# Convenience wrappers — bootstrap-specific, not promoted to _lib.sh because
# only this script invokes rucio-admin.
ra()      { _exec rucio      rucio-admin -S userpass -u ddmlab --password secret "$@"; }
ra_oidc() { _exec rucio-oidc rucio-admin -S userpass -u ddmlab --password secret "$@"; }

# ── Infrastructure Readiness ────────────────────────────────────────────────

wait_for_infrastructure() {
    echo "=== Waiting for Rucio and Keycloak ==="
    for i in $(seq 1 30); do
        code=$(_http_probe_local 8090 /ping)
        [[ "$code" == "200" ]] && { echo "  ✓ rucio-oidc ready"; break; }
        echo "  [$i] rucio-oidc HTTP $code — waiting..."; sleep 5
    done

    for i in $(seq 1 30); do
        code=$(_exec rucio-oidc curl -s -o /dev/null -w '%{http_code}' \
            https://keycloak:8443/realms/rucio/.well-known/openid-configuration 2>/dev/null) || true
        [[ "$code" == "200" ]] && { echo "  ✓ Keycloak ready"; break; }
        echo "  [$i] Keycloak HTTP $code — waiting..."; sleep 5
    done
}

restart_storm_nodes() {
    local label=$1
    echo "=== Restarting StoRM ($label) ==="
    _restart storm1 storm2

    case "$RUNTIME" in
        compose)
            echo "  Waiting for StoRM health checks..."
            for i in $(seq 1 20); do
                s1=$(docker inspect --format='{{.State.Health.Status}}' compose-storm1-1 2>/dev/null || echo unknown)
                s2=$(docker inspect --format='{{.State.Health.Status}}' compose-storm2-1 2>/dev/null || echo unknown)
                if [[ "$s1" == "healthy" && "$s2" == "healthy" ]]; then
                    echo "  ✓ All StoRM nodes healthy"; return 0
                fi
                echo "  [$i] storm1=$s1 storm2=$s2 — waiting..."; sleep 10
            done ;;
        k8s)
            # _restart already waited via `kubectl rollout status`.
            echo "  ✓ StoRM rollouts complete"
            ;;
    esac
}

# ── Identity & Account Setup ───────────────────────────────────────────────

setup_accounts_and_identities() {
    echo "=== Configuring Rucio Accounts ==="

    ra account add --type SERVICE --email ddmlab@rucio ddmlab || true
    ra identity add --type USERPASS --id ddmlab --email ddmlab@rucio --account ddmlab --password secret || true
    ra account add-attribute ddmlab --key admin --value True || true
    ra account update --account ddmlab --key type --value SERVICE || true

    ra_oidc account add --type SERVICE --email ddmlab@rucio ddmlab || true
    ra_oidc identity add --type USERPASS --id ddmlab --email ddmlab@rucio --account ddmlab --password secret || true
    ra_oidc account add-attribute ddmlab --key admin --value True || true
    ra_oidc account update --account ddmlab --key type --value SERVICE || true

    ra_oidc account add --type USER --email randomaccount@rucio randomaccount || true

    echo "  Verifying Keycloak token endpoint..."
    AUTH=$(echo -n "rucio-oidc:rucio-oidc-secret" | base64)
    for i in $(seq 1 12); do
        code=$(_exec rucio-oidc curl -s -o /dev/null -w '%{http_code}' \
            -X POST https://keycloak:8443/realms/rucio/protocol/openid-connect/token \
            -H "Authorization: Basic $AUTH" -d "grant_type=password&username=randomaccount&password=secret" 2>/dev/null) || true
        [[ "$code" == "200" ]] && break
        sleep 5
    done

    echo "  Registering OIDC identity for randomaccount..."
    _exec rucio-oidc python3 -c "
import urllib.request, urllib.parse, json, base64
from rucio.core.identity import add_identity, add_account_identity
from rucio.common.types import InternalAccount
from rucio.common import exception

try:
    data = urllib.parse.urlencode({'grant_type':'password','username':'randomaccount','password':'secret'}).encode()
    _auth = base64.b64encode(b'rucio-oidc:rucio-oidc-secret').decode()
    req = urllib.request.Request('https://keycloak:8443/realms/rucio/protocol/openid-connect/token',
        data=data, headers={'Authorization': f'Basic {_auth}'})
    resp = json.loads(urllib.request.urlopen(req).read())
    claims = json.loads(base64.urlsafe_b64decode(resp['access_token'].split('.')[1] + '=='))
    identity = claims['iss'] + '#' + claims['sub']

    try: add_identity(identity, 'OIDC', 'randomaccount@rucio')
    except exception.Duplicate: pass

    try: add_account_identity(identity, 'OIDC', InternalAccount('randomaccount'), 'randomaccount@rucio')
    except exception.Duplicate: pass
    print(f'  ✓ Identity registered: {identity}')
except Exception as e:
    print(f'  ⚠ Registration failed: {e}')
"
}

# ── RSE Configuration ───────────────────────────────────────────────────────

configure_rses() {
    local cmd=$1
    local fts_endpoint=$2
    local label=$3
    echo "=== Configuring RSEs on $label ==="

    # XRD1/XRD2 on both instances (GSI auth works everywhere).
    for rse in XRD1 XRD2; do
        local host=$(echo "$rse" | tr '[:upper:]' '[:lower:]')
        $cmd rse add "$rse" || true
        $cmd rse set-attribute --rse "$rse" --key fts --value "$fts_endpoint"
        $cmd rse set-attribute --rse "$rse" --key verify_checksum --value True
        $cmd rse add-protocol "$rse" --scheme root --hostname "$host" --port 1094 --prefix //rucio \
            --impl rucio.rse.protocols.gfal.Default \
            --domain-json '{"wan":{"read":1,"write":1,"delete":1,"third_party_copy_read":1,"third_party_copy_write":1},"lan":{"read":1,"write":1,"delete":1}}'
    done

    # WEBDAV on both instances.
    for rse in WEBDAV1 WEBDAV2; do
        local host=$(echo "$rse" | tr '[:upper:]' '[:lower:]')
        $cmd rse add "$rse" || true
        $cmd rse set-attribute --rse "$rse" --key fts --value "$fts_endpoint"
        $cmd rse add-protocol "$rse" --scheme davs --hostname "$host" --port 443 --prefix /webdav \
            --impl rucio.rse.protocols.gfal.Default \
            --domain-json '{"wan":{"read":1,"write":1,"delete":1,"third_party_copy_read":1,"third_party_copy_write":1},"lan":{"read":1,"write":1,"delete":1}}'
    done

    # OIDC-only RSEs: XRD3/XRD4 (SciTokens) and STORM1/STORM2 (OIDC bearer).
    if [[ "$label" == "Rucio-OIDC" ]]; then

        for rse in XRD3 XRD4; do
            local host=$(echo "$rse" | tr '[:upper:]' '[:lower:]')
            $cmd rse add "$rse" || true
            $cmd rse set-attribute --rse "$rse" --key fts --value "$FTS_OIDC"
            $cmd rse set-attribute --rse "$rse" --key oidc_support --value True
            $cmd rse set-attribute --rse "$rse" --key auth_type --value OIDC
            $cmd rse set-attribute --rse "$rse" --key audience --value "https://${host}:1094"
            $cmd rse set-attribute --rse "$rse" --key verify_checksum --value False
            $cmd rse add-protocol "$rse" --scheme davs --hostname "$host" --port 1094 --prefix /data \
                --impl rucio.rse.protocols.gfal.Default \
                --domain-json '{"wan":{"read":1,"write":1,"delete":1,"third_party_copy_read":1,"third_party_copy_write":1},"lan":{"read":1,"write":1,"delete":1}}'
        done

        for rse in STORM1 STORM2; do
            local host=$(echo "$rse" | tr '[:upper:]' '[:lower:]')
            $cmd rse add "$rse" || true
            $cmd rse set-attribute --rse "$rse" --key fts --value "$FTS_OIDC"
            $cmd rse set-attribute --rse "$rse" --key oidc_support --value True
            $cmd rse set-attribute --rse "$rse" --key auth_type --value OIDC
            $cmd rse set-attribute --rse "$rse" --key audience --value "$host"
            $cmd rse set-attribute --rse "$rse" --key verify_checksum --value False

            local scheme="davs" port="8443"
            local domains='{"wan":{"read":1,"write":1,"delete":1,"third_party_copy_read":1,"third_party_copy_write":1},"lan":{"read":1,"write":1,"delete":1}}'

            if [[ "$rse" == "STORM1" ]]; then
                scheme="http"; port="8085"
                domains='{"wan":{"read":0,"write":0,"delete":0,"third_party_copy_read":1,"third_party_copy_write":0},"lan":{"read":0,"write":0,"delete":0}}'
            fi

            $cmd rse add-protocol "$rse" --scheme "$scheme" --hostname "$host" --port "$port" --prefix /data \
                --impl rucio.rse.protocols.gfal.Default --domain-json "$domains"
        done

        for rse in TEAPOT1 TEAPOT2; do
            local instance=$(echo "$rse" | tr '[:upper:]' '[:lower:]')
            $cmd rse add "$rse" || true
            $cmd rse set-attribute --rse "$rse" --key fts --value "$FTS_OIDC"
            $cmd rse set-attribute --rse "$rse" --key oidc_support --value True
            $cmd rse set-attribute --rse "$rse" --key auth_type --value OIDC
            $cmd rse set-attribute --rse "$rse" --key audience --value "teapot"
            $cmd rse set-attribute --rse "$rse" --key verify_checksum --value False
            $cmd rse add-protocol "$rse" --scheme davs \
                --hostname "${instance}" --port 8081 --prefix /data \
                --impl rucio.rse.protocols.gfal.Default \
                --domain-json '{"wan":{"read":1,"write":1,"delete":1,"third_party_copy_read":1,"third_party_copy_write":1},"lan":{"read":1,"write":1,"delete":1}}'
        done
        $cmd rse add-distance TEAPOT1 TEAPOT2 --distance 1 || true
        $cmd rse add-distance TEAPOT2 TEAPOT1 --distance 1 || true
    fi

    # Distances — XRD1/2 and WEBDAV on both; OIDC-only pairs only on OIDC.
    $cmd rse add-distance XRD1 XRD2 --distance 1 || true
    $cmd rse add-distance XRD2 XRD1 --distance 1 || true
    $cmd rse add-distance WEBDAV1 WEBDAV2 --distance 1 || true
    $cmd rse add-distance WEBDAV2 WEBDAV1 --distance 1 || true

    if [[ "$label" == "Rucio-OIDC" ]]; then
        $cmd rse add-distance STORM1 STORM2 --distance 1 || true
        $cmd rse add-distance STORM2 STORM1 --distance 1 || true
        $cmd rse add-distance XRD3 XRD4 --distance 1 || true
        $cmd rse add-distance XRD4 XRD3 --distance 1 || true
    fi
}

# ── S3 / MinIO RSE Configuration ──────────────────────────────────────────

_register_minio_rses() {
    local cmd=$1
    local extra_accounts=${2:-}

    for rse in MINIO1 MINIO2; do
        local host
        host=$(echo "$rse" | tr '[:upper:]' '[:lower:]')
        $cmd rse add "$rse" || true
        $cmd rse set-attribute --rse "$rse" --key fts              --value "$FTS"
        $cmd rse set-attribute --rse "$rse" --key sign_url         --value s3
        $cmd rse set-attribute --rse "$rse" --key skip_upload_stat --value True
        $cmd rse set-attribute --rse "$rse" --key verify_checksum  --value False
        $cmd rse set-attribute --rse "$rse" --key strict_copy      --value True
        $cmd rse set-attribute --rse "$rse" --key s3_url_style     --value path
        $cmd rse add-protocol "$rse" \
            --scheme https --hostname "$host-tls" --port 9000 \
            --prefix /fts-test \
            --impl rucio.rse.protocols.gfal.Default \
            --domain-json '{"wan":{"read":1,"write":1,"delete":1,"third_party_copy_read":1,"third_party_copy_write":1},"lan":{"read":1,"write":1,"delete":1}}'
        $cmd account set-limits ddmlab "$rse" -1 || true
        $cmd account set-limits root   "$rse" -1 || true
        [[ -n "$extra_accounts" ]] && \
            $cmd account set-limits "$extra_accounts" "$rse" -1 || true
    done

    $cmd rse add-distance MINIO1 MINIO2 --distance 1 || true
    $cmd rse add-distance MINIO2 MINIO1 --distance 1 || true
    $cmd rse add-distance XRD1   MINIO1 --distance 1 || true
    $cmd rse add-distance MINIO1 XRD2   --distance 1 || true
    $cmd rse add-distance XRD2   MINIO1 --distance 1 || true
    $cmd rse add-distance MINIO1 XRD1   --distance 1 || true
}

_write_rse_accounts() {
    local svc=$1
    local m1_id m2_id tmp_cfg
    m1_id=$(_exec "$svc" python3 -c "
from rucio.core.rse import get_rse_id; print(get_rse_id('MINIO1'))")
    m2_id=$(_exec "$svc" python3 -c "
from rucio.core.rse import get_rse_id; print(get_rse_id('MINIO2'))")

    tmp_cfg=$(mktemp)
    cat > "$tmp_cfg" << ACCOUNTS
{
    "${m1_id}": {
        "access_key": "minioadmin",
        "secret_key": "minioadmin",
        "signature_version": "s3v4",
        "region": "us-east-1"
    },
    "${m2_id}": {
        "access_key": "minioadmin",
        "secret_key": "minioadmin",
        "signature_version": "s3v4",
        "region": "us-east-1"
    }
}
ACCOUNTS

    case "$RUNTIME" in
        compose)
            docker cp "$tmp_cfg" "compose-${svc}-1:/opt/rucio/etc/rse-accounts.cfg"
            ;;
        k8s)
            local container=""
            [[ "$svc" == "rucio" || "$svc" == "rucio-oidc" ]] && container="-c $svc"
            local pod
            pod=$(kubectl -n "$K8S_NAMESPACE" get pods -l "app.kubernetes.io/name=${svc}" \
                -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || \
                kubectl -n "$K8S_NAMESPACE" get pods -l "app=${svc}" \
                -o jsonpath='{.items[0].metadata.name}')
            kubectl -n "$K8S_NAMESPACE" cp "$tmp_cfg" \
                "${pod}:/opt/rucio/etc/rse-accounts.cfg" $container
            ;;
    esac

    rm -f "$tmp_cfg"
    echo "  ✓ rse-accounts.cfg written to $svc (MINIO1=$m1_id MINIO2=$m2_id)"
}

setup_s3_rses() {
    echo "=== Configuring S3/MinIO RSEs ==="

    # ── Per-instance RSE registration ──────────────────────────────────────
    _register_minio_rses "ra"
    _register_minio_rses "ra_oidc" "randomaccount"

    # ── rse-accounts.cfg ───────────────────────────────────────────────────
    echo "  Writing rse-accounts.cfg..."

    _write_rse_accounts "rucio"
    _write_rse_accounts "rucio-oidc"
}

# ── FTS & Delegation ───────────────────────────────────────────────────────

setup_fts_oidc_provider() {
    echo "=== Registering Keycloak in FTS Database ==="

    echo "  Waiting for fts.t_token_provider schema..."
    for i in $(seq 1 60); do
        if _exec ftsdb-oidc mysql -h 127.0.0.1 --protocol=tcp -ufts -pfts fts \
            -e "SELECT 1 FROM t_token_provider LIMIT 1" >/dev/null 2>&1; then
            echo "  ✓ Schema ready"
            break
        fi
        if [ "$i" = "60" ]; then
            echo "  ✗ Schema never appeared — fts-oidc may not have started"
            _exec ftsdb-oidc mysql -h 127.0.0.1 --protocol=tcp -ufts -pfts fts \
                -e "SHOW TABLES" || true
            exit 1
        fi
        sleep 5
    done

    _exec ftsdb-oidc mysql -h 127.0.0.1 --protocol=tcp -ufts -pfts fts -e "
    INSERT IGNORE INTO t_token_provider (name, issuer, client_id, client_secret)
    VALUES
      ('keycloak-rucio',       'https://keycloak:8443/realms/rucio',  'rucio-oidc', 'rucio-oidc-secret'),
      ('keycloak-rucio-slash', 'https://keycloak:8443/realms/rucio/', 'rucio-oidc', 'rucio-oidc-secret');"

    echo "  Restarting fts-oidc..."
    _restart fts-oidc
    for i in $(seq 1 30); do
        code=$(_exec fts-oidc curl -sk -o /dev/null -w '%{http_code}' \
            https://localhost:8446/whoami 2>/dev/null) || code=0
        [[ "$code" == "200" || "$code" == "403" ]] && { echo "  ✓ fts-oidc ready"; break; }
        sleep 5
    done
}

# ── Scopes & Quotas ─────────────────────────────────────────────────────────

setup_scopes_and_quotas() {
    echo "=== Configuring Scopes and Quotas ==="

    ra scope add --account root --scope test || true
    ra_oidc scope add --account root --scope test || true
    ra_oidc scope add --account randomaccount --scope randomaccount || true
    ra scope add --account ddmlab --scope ddmlab || true
    ra_oidc scope add --account ddmlab --scope ddmlab || true

    for rse in XRD1 XRD2 WEBDAV1 WEBDAV2; do
        ra account set-limits root "$rse" -1 || true
        ra account set-limits ddmlab "$rse" -1 || true
    done

    for rse in STORM1 STORM2 XRD3 XRD4; do
        ra_oidc account set-limits root "$rse" -1 || true
        ra_oidc account set-limits randomaccount "$rse" -1 || true
        ra_oidc account set-limits ddmlab "$rse" -1 || true
    done

    for rse in TEAPOT1 TEAPOT2; do
        ra_oidc account set-limits randomaccount "$rse" -1 || true
        ra_oidc account set-limits ddmlab "$rse" -1 || true
    done
}

# ── Main ──────────────────────────────────────────────────────────────────────

main() {
    wait_for_infrastructure
    restart_storm_nodes "Initial Startup"
    setup_accounts_and_identities
    configure_rses "ra"      "$FTS"      "Rucio-Classic"
    configure_rses "ra_oidc" "$FTS"      "Rucio-OIDC"
    setup_s3_rses
    setup_scopes_and_quotas
    setup_fts_oidc_provider
    restart_storm_nodes "Final JWKS Sync"

    echo -e "\n=== Bootstrap Complete ==="
}

main
