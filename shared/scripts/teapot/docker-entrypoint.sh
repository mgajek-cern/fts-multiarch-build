#!/bin/bash
set -e

# Register any newly mounted CA certs into the system trust store
update-ca-certificates --fresh 2>/dev/null || true

# Ensure the data volume is writable by the teapot user
chown -R teapot:teapot /data 2>/dev/null || true

# Copy read-only mounted config to writable location
CONFIG=/etc/teapot/config.ini

echo "=== Teapot config ==="
grep -E "^(trusted_OP|mapping|mapping_file|Teapot_CA|hostname|port) " "$CONFIG" || true
echo "====================="

exec python3 /usr/share/teapot/teapot.py
