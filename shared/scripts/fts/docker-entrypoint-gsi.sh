#!/usr/bin/env bash
set -euo pipefail

echo "waiting for database..."
/usr/local/bin/wait-for-it.sh -h ftsdb -p 3306 -t 3600

# initialise / upgrade the database — ignore ALL errors (duplicate keys etc.)
echo "upgrading database..."
/usr/share/fts/fts-database-upgrade.py -y 2>&1 || echo "DB upgrade finished (errors ignored)"

# Check for specific CA hash if needed for debugging
ls /etc/grid-security/certificates/5fca1cb1* 2>/dev/null || true
ls /etc/grid-security/certificates/b96dc756* 2>/dev/null || true

update-ca-trust 2>/dev/null || true

echo "starting services..."

(/usr/sbin/fts_server &) || true
(/usr/sbin/fts_token &) || true
(/usr/sbin/fts_activemq &) || true
(/usr/sbin/fts_qos &) || true

exec /usr/sbin/httpd -DFOREGROUND
