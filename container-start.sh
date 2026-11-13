#!/bin/bash
set -euo pipefail

: "${PANEL_USER:=admin}"
: "${PANEL_PASS:=changeme-panel}"
: "${PANEL_SECRET:=wp-panel-gate}"

export PANEL_GATE_TOKEN="$(
  printf '%s' "${PANEL_USER}:${PANEL_PASS}:${PANEL_SECRET}" | sha256sum | awk '{print $1}'
)"

mkdir -p /config /database

# Apply saved (or default) tool paths + PHP version before services start.
if [ -d /opt/panel ]; then
  (
    cd /opt/panel
    python3 -c 'from app import ensure_tool_paths_applied, ensure_php_version_applied; ensure_tool_paths_applied(); ensure_php_version_applied(restart_fpm=False)'
  ) || {
    # Fallback gate so tools stay protected even if settings apply fails.
    cat > /etc/apache2/panel-gate.rewrite <<EOF
# Auto-generated fallback — requires panel login cookie before /filebrowser and /phpmyadmin
RewriteCond %{REQUEST_URI} ^/(filebrowser|phpmyadmin)(/|\$) [NC]
RewriteCond %{HTTP_COOKIE} !panel_gate=${PANEL_GATE_TOKEN}
RewriteRule ^ /panel/login?next=%{REQUEST_URI} [R=302,L,QSA]
EOF
    cat > /etc/apache2/panel-tool-paths.rewrite <<'EOF'
# Auto-generated fallback
RewriteRule ^/phpmyadmin - [L]
RewriteRule ^/filebrowser - [L]
EOF
  }
else
  cat > /etc/apache2/panel-gate.rewrite <<EOF
# Auto-generated — requires panel login cookie before /filebrowser and /phpmyadmin
RewriteCond %{REQUEST_URI} ^/(filebrowser|phpmyadmin)(/|\$) [NC]
RewriteCond %{HTTP_COOKIE} !panel_gate=${PANEL_GATE_TOKEN}
RewriteRule ^ /panel/login?next=%{REQUEST_URI} [R=302,L,QSA]
EOF
  cat > /etc/apache2/panel-tool-paths.rewrite <<'EOF'
RewriteRule ^/phpmyadmin - [L]
RewriteRule ^/filebrowser - [L]
EOF
fi

exec /usr/bin/supervisord -n
