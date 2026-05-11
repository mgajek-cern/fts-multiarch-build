#!/usr/bin/env bash
# generate-certs.sh — generate all runtime certificates and Java truststores
# Run from repo root. Requires certs/rucio_ca.pem and certs/rucio_ca.key.pem.

set -euo pipefail

# ── Global Config ───────────────────────────────────────────────────────────
CERTS="certs"
STORM_IMAGE="ghcr.io/italiangrid/storm-webdav:v1.12.0"

# ── Helpers ─────────────────────────────────────────────────────────────────

write_ext_file() {
  local out=$1; local dns_list=$2; local profile=$3; local eku
  case $profile in
    server) eku="serverAuth" ;;
    client) eku="clientAuth" ;;
    both)   eku="serverAuth, clientAuth" ;;
  esac
  {
    echo "[ v3_req ]"
    echo "keyUsage         = digitalSignature, keyEncipherment"
    echo "extendedKeyUsage = $eku"
    echo "subjectAltName   = @alt_names"
    echo -e "\n[ alt_names ]"
    local i=1; IFS=',' read -ra names <<< "$dns_list"
    for n in "${names[@]}"; do echo "DNS.$i = ${n// /}"; i=$((i+1)); done
    # echo "IP.1 = 127.0.0.1"
  } > "$out"
}

mint_cert() {
  local base=$1; local cn=$2; local extfile=$3
  openssl req -nodes -newkey rsa:2048 -keyout "$CERTS/${base}key.pem" \
    -out "$CERTS/${base}cert.csr" -subj "/CN=${cn}" 2>/dev/null
  openssl x509 -req -days 365 -in "$CERTS/${base}cert.csr" \
    -CA "$CERTS/rucio_ca.pem" -CAkey "$CERTS/rucio_ca.key.pem" -CAcreateserial \
    -extfile "$extfile" -extensions v3_req -out "$CERTS/${base}cert.pem" 2>/dev/null
}

# ── Logic Blocks ────────────────────────────────────────────────────────────

generate_service_certs() {
    echo "=== Generating Service Certificates ==="

    # FTS (Shared by fts and fts-oidc)
    write_ext_file /tmp/fts-ext.cnf "fts,fts-oidc,localhost" both
    mint_cert "host" "fts" /tmp/fts-ext.cnf

    cat "$CERTS/hostcert.pem" "$CERTS/hostkey.pem" > "$CERTS/hostcert_with_key.pem"
    chmod 600 "$CERTS/hostkey.pem" "$CERTS/hostcert_with_key.pem"

    # XRootD (Shared by xrd1, xrd2, xrd3, xrd4)
    for host in xrd1 xrd2 xrd3 xrd4; do
        write_ext_file "/tmp/${host}-ext.cnf" "${host},${host}.rucio-testbed.svc.cluster.local,localhost" both
        mint_cert "${host}" "${host}" "/tmp/${host}-ext.cnf"
        chmod 644 "$CERTS/${host}key.pem"
    done

    # StoRM nodes
    for host in storm1 storm2; do
        write_ext_file "/tmp/${host}-ext.cnf" "${host},localhost" both
        mint_cert "${host}" "${host}" "/tmp/${host}-ext.cnf"
        chmod 644 "$CERTS/${host}key.pem"
    done

    write_ext_file /tmp/teapot-ext.cnf "teapot,localhost" server
    mint_cert "teapot" "teapot" /tmp/teapot-ext.cnf
    chmod 644 "$CERTS/teapotkey.pem"

    # Storm-WebDAV localhost cert (for Teapot's internal Storm-WebDAV instance)
    write_ext_file /tmp/storm-webdav-localhost-ext.cnf "localhost" server
    mint_cert "storm-webdav-localhost" "localhost" /tmp/storm-webdav-localhost-ext.cnf
    chmod 644 "$CERTS/storm-webdav-localhostkey.pem"

    # WebDAV nodes
    for host in webdav1 webdav2; do
        write_ext_file "/tmp/${host}-ext.cnf" "${host},localhost" server
        mint_cert "${host}" "${host}" "/tmp/${host}-ext.cnf"
        chmod 600 "$CERTS/${host}key.pem"
    done

    # Keycloak
    write_ext_file /tmp/keycloak-ext.cnf "keycloak,localhost" server
    mint_cert "keycloak" "keycloak" /tmp/keycloak-ext.cnf
    chmod 644 "$CERTS/keycloakkey.pem"
}

setup_trust_anchors() {
    echo "=== Preparing Trust Anchors (XRootD-safe mode) ==="
    mkdir -p "$CERTS"

    HASH_NEW=$(openssl x509 -noout -hash -in "$CERTS/rucio_ca.pem")
    HASH_OLD=$(openssl x509 -noout -subject_hash_old -in "$CERTS/rucio_ca.pem")

    for H in "$HASH_NEW" "$HASH_OLD"; do
        cp "$CERTS/rucio_ca.pem" "$CERTS/${H}.0"
        chmod 644 "$CERTS/${H}.0"
    done

    CA_SUBJECT=$(openssl x509 -noout -subject -nameopt compat -in "$CERTS/rucio_ca.pem" | sed 's/^subject=//; s/, /\//g')
    [[ "$CA_SUBJECT" != /* ]] && CA_SUBJECT="/$CA_SUBJECT"

    for H in "$HASH_NEW" "$HASH_OLD"; do
        cat > "$CERTS/${H}.signing_policy" <<EOF
access_id_CA      X509    '${CA_SUBJECT}'
pos_rights        globus  CA:sign
cond_subjects     globus  '/*'
EOF
        chmod 644 "$CERTS/${H}.signing_policy"
    done

    chmod 755 "$CERTS"

    echo "✔ Trust anchors fully materialized (correct permissions)"
}

generate_java_stores() {
    echo "=== Generating Java Truststores ==="

    # Clean previous artifacts
    rm -f "$CERTS/storm-cacerts"

    # Ensure certs dir exists
    mkdir -p "$CERTS"

    # ── 1. Try Docker FIRST (authoritative path, same as CI) ────────────────
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        echo "Docker detected. Attempting truststore generation via StoRM image..."

        if ! docker image inspect "$STORM_IMAGE" >/dev/null 2>&1; then
            docker pull --platform linux/amd64 "$STORM_IMAGE" >/dev/null
        fi

        if docker run --rm --platform linux/amd64 -u root \
            -v "$(pwd)/$CERTS:/certs" \
            --entrypoint /bin/sh "$STORM_IMAGE" -c "
                set -e
                cp /opt/java/openjdk/lib/security/cacerts /certs/storm-cacerts
                keytool -import -trustcacerts -noprompt \
                  -alias rucio-ca \
                  -file /certs/rucio_ca.pem \
                  -keystore /certs/storm-cacerts \
                  -storepass changeit
            " 2>/dev/null; then

            echo "✓ Truststore generated via Docker (CI-parity path)"

            # Fix Docker root-owned files
            sudo chown -R "$(id -u):$(id -g)" "$CERTS" 2>/dev/null || true
            chmod 644 "$CERTS/storm-cacerts"

            return 0
        else
            echo "⚠ Docker run failed. Falling back to local keytool..."
        fi
    else
        echo "Docker not available. Using local keytool fallback..."
    fi

    # ── 2. Fallback: LOCAL keytool (minimal + strict) ───────────────────────
    if ! command -v keytool >/dev/null 2>&1; then
        echo "ERROR: keytool not found and Docker unavailable/failed."
        exit 1
    fi

    # Require a valid JAVA_HOME or resolvable one
    if [[ -z "${JAVA_HOME:-}" ]]; then
        if command -v java >/dev/null 2>&1; then
            JAVA_HOME="$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")"
        else
            echo "ERROR: JAVA_HOME not set and cannot infer Java installation."
            exit 1
        fi
    fi

    CACERTS="$JAVA_HOME/lib/security/cacerts"

    if [[ ! -f "$CACERTS" ]]; then
        echo "ERROR: Cannot find cacerts at $CACERTS"
        exit 1
    fi

    echo "Using local Java truststore base: $CACERTS"

    cp "$CACERTS" "$CERTS/storm-cacerts"
    chmod 644 "$CERTS/storm-cacerts"

    keytool -import -trustcacerts -noprompt \
      -alias rucio-ca \
      -file "$CERTS/rucio_ca.pem" \
      -keystore "$CERTS/storm-cacerts" \
      -storepass changeit

    echo "✓ Truststore generated via local keytool"
}

cleanup_intermediaries() {
    echo "=== Cleaning up CSRs and Temporary Files ==="
    rm -f "$CERTS"/*.csr "$CERTS"/*.srl /tmp/*-ext.cnf
}

# ── Main Entry Point ────────────────────────────────────────────────────────

main() {
    if [[ ! -f "$CERTS/rucio_ca.pem" || ! -f "$CERTS/rucio_ca.key.pem" ]]; then
        echo "ERROR: CA files missing in $CERTS/"
        exit 1
    fi

    generate_service_certs
    setup_trust_anchors
    generate_java_stores
    cleanup_intermediaries

    echo -e "\n=== Certificate Generation Complete ==="
}

main
