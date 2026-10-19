#!/bin/bash
set -euo pipefail

: "${PANEL_USER:=admin}"
: "${PANEL_PASS:=changeme-panel}"
: "${PANEL_SECRET:=wp-panel-gate}"

export PANEL_GATE_TOKEN="$(
  printf '%s' "${PANEL_USER}:${PANEL_PASS}:${PANEL_SECRET}" | sha256sum | awk '{print $1}'
)"

# Gate rewrite must exist before Apache starts.
cat > /etc/apache2/panel-gate.rewrite <<EOF
# Auto-generated — requires panel login cookie before /filebrowser and /phpmyadmin
RewriteCond %{REQUEST_URI} ^/(filebrowser|phpmyadmin)(/|\$) [NC]
RewriteCond %{HTTP_COOKIE} !panel_gate=${PANEL_GATE_TOKEN}
RewriteRule ^ /panel/login?next=%{REQUEST_URI} [R=302,L,QSA]
EOF

exec /usr/bin/supervisord -n
