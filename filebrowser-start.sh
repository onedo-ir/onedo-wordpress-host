#!/bin/bash
# Wait for init to create the DB, then run FileBrowser on localhost.
# Runs as www-data so uploads/extracts match PHP-FPM ownership and
# WordPress can install plugins/themes without permission errors.
set -euo pipefail

DB="/database/filebrowser.db"
mkdir -p /database /config

FILEBROWSER_BASEURL="/filebrowser"
if [ -f /config/panel-paths.env ]; then
  # shellcheck disable=SC1091
  # Written only by the admin panel / ensure_tool_paths_applied.
  . /config/panel-paths.env
fi
: "${FILEBROWSER_BASEURL:=/filebrowser}"

echo "[filebrowser] waiting for ${DB}..."
for i in $(seq 1 60); do
    if [ -f "$DB" ]; then
        break
    fi
    sleep 1
done

if [ ! -f "$DB" ]; then
    echo "[filebrowser] ${DB} missing after 60s; initializing empty config"
    /usr/local/bin/filebrowser config init \
        --database "$DB" \
        --address 127.0.0.1 \
        --port 8080 \
        --baseurl "${FILEBROWSER_BASEURL}" \
        --root /var/www
fi

# Keep DB baseurl aligned with panel settings (survives volume upgrades).
/usr/local/bin/filebrowser config set \
    --database "$DB" \
    --address 127.0.0.1 \
    --port 8080 \
    --baseurl "${FILEBROWSER_BASEURL}" \
    --root /var/www >/dev/null 2>&1 || true

chown www-data:www-data /database "$DB" 2>/dev/null || true
chmod 775 /database 2>/dev/null || true
chmod 664 "$DB" 2>/dev/null || true
# Keep web tree writable by the same user PHP-FPM uses.
chown -R www-data:www-data /var/www 2>/dev/null || true

# Root is /var/www (WordPress under html/ and related web files).
exec su -s /bin/bash www-data -c "exec /usr/local/bin/filebrowser \
    -a 127.0.0.1 \
    -p 8080 \
    --baseurl '${FILEBROWSER_BASEURL}' \
    --database '$DB' \
    --root /var/www"
