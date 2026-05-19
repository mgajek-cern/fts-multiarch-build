#!/usr/bin/env bash
set -euo pipefail

echo "Preparing host TLS certs..."

# Create directory for host certs (internal to container)
mkdir -vp /etc/grid-security/xrd

# Copy from RO mount to writable location
if [[ -f /tmp/xrdcert.pem ]]; then
    cp /tmp/xrdcert.pem /etc/grid-security/xrd/xrdcert.pem
fi

if [[ -f /tmp/xrdkey.pem ]]; then
    cp /tmp/xrdkey.pem /etc/grid-security/xrd/xrdkey.pem
    chmod 400 /etc/grid-security/xrd/xrdkey.pem
fi

# Ownership is handled by fsGroup 1000 usually, but we ensure it here
chown -R xrootd:xrootd /etc/grid-security/xrd || true

echo "✔ CA trust bundle already prepared by init container"

mkdir -p /data
chown -R xrootd:xrootd /data || true

# Check for specific CA hash if needed for debugging
ls /etc/grid-security/certificates/5fca1cb1* 2>/dev/null || true
ls /etc/grid-security/certificates/b96dc756* 2>/dev/null || true

update-ca-trust 2>/dev/null || true

exec /docker-entrypoint.sh
