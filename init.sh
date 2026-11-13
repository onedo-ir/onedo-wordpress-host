#!/bin/bash
set -e

STATE_FILE="/var/lib/bootstrap.done"
WP_HTACCESS="/var/www/html/.htaccess"

# Pretty permalinks 404 without rewrite rules. Keep this on every start
# because the html volume can exist without a usable .htaccess.
ensure_wordpress_htaccess() {
    local block
    block="$(cat <<'EOF'
# BEGIN WordPress
<IfModule mod_rewrite.c>
RewriteEngine On
RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]
RewriteBase /
RewriteRule ^index\.php$ - [L]
RewriteCond %{REQUEST_FILENAME} !-f
RewriteCond %{REQUEST_FILENAME} !-d
RewriteRule . /index.php [L]
</IfModule>
# END WordPress

# BEGIN Performance
<IfModule mod_expires.c>
ExpiresActive On
ExpiresByType text/css "access plus 7 days"
ExpiresByType application/javascript "access plus 7 days"
ExpiresByType image/jpeg "access plus 30 days"
ExpiresByType image/png "access plus 30 days"
ExpiresByType image/webp "access plus 30 days"
ExpiresByType image/svg+xml "access plus 30 days"
ExpiresByType font/woff2 "access plus 30 days"
</IfModule>
# END Performance
EOF
)"

    if [ ! -d /var/www/html ]; then
        return 0
    fi

    if [ -f "$WP_HTACCESS" ]; then
        # Drop a previous WordPress/Performance block so a wrong RewriteBase /
        # RewriteEngine Off left on the html volume cannot keep permalinks 404.
        sed -i '/# BEGIN WordPress/,/# END WordPress/d' "$WP_HTACCESS"
        sed -i '/# BEGIN Performance/,/# END Performance/d' "$WP_HTACCESS"
        sed -i '/^[[:space:]]*RewriteEngine[[:space:]]\+Off[[:space:]]*$/d' "$WP_HTACCESS"
        printf '\n%s\n' "$block" >> "$WP_HTACCESS"
        echo "[INIT] Refreshed WordPress .htaccess rewrite rules"
    else
        printf '%s\n' "$block" > "$WP_HTACCESS"
        echo "[INIT] Created WordPress .htaccess for permalinks"
    fi
    chown www-data:www-data "$WP_HTACCESS" 2>/dev/null || true
}

flush_permalinks() {
    if [ ! -f /var/www/html/wp-config.php ]; then
        return 0
    fi

    local i
    for i in $(seq 1 30); do
        if wp db check --allow-root --path=/var/www/html >/dev/null 2>&1; then
            if wp core is-installed --allow-root --path=/var/www/html >/dev/null 2>&1; then
                wp rewrite flush --hard --allow-root --path=/var/www/html >/dev/null 2>&1 || true
                echo "[INIT] Flushed WordPress rewrite rules"
            fi
            return 0
        fi
        sleep 1
    done
    echo "[INIT] Skipped rewrite flush (database not ready)"
}

# Keep wp-config lean: fewer revisions, capped memory, no file editor.
ensure_wp_performance_config() {
    local cfg="/var/www/html/wp-config.php"
    if [ ! -f "$cfg" ]; then
        return 0
    fi

    if grep -q "WP_PERF_TUNING" "$cfg" 2>/dev/null; then
        return 0
    fi

    local block_file
    block_file="$(mktemp)"
    cat > "$block_file" <<'EOF'

/* WP_PERF_TUNING */
define('WP_MEMORY_LIMIT', '128M');
define('WP_MAX_MEMORY_LIMIT', '256M');
define('WP_POST_REVISIONS', 5);
define('EMPTY_TRASH_DAYS', 7);
define('AUTOSAVE_INTERVAL', 120);
define('WP_CRON_LOCK_TIMEOUT', 60);
define('DISABLE_WP_CRON', true);
define('CONCATENATE_SCRIPTS', true);
define('DISALLOW_FILE_EDIT', true);
/* END WP_PERF_TUNING */

EOF

    if grep -q "wp-settings.php" "$cfg"; then
        # Insert immediately before WordPress bootstrap require
        awk -v bf="$block_file" '
            /wp-settings\.php/ && !done {
                while ((getline line < bf) > 0) print line
                close(bf)
                done=1
            }
            { print }
        ' "$cfg" > "${cfg}.tmp" && mv "${cfg}.tmp" "$cfg"
    else
        cat "$block_file" >> "$cfg"
    fi
    rm -f "$block_file"
    echo "[INIT] Applied WordPress performance constants"
}

# Edge terminates TLS and talks HTTP to this container. Without this,
# WordPress builds http:// asset URLs → browser mixed-content blocks.
ensure_wp_reverse_proxy_ssl() {
    local cfg="/var/www/html/wp-config.php"
    if [ ! -f "$cfg" ]; then
        return 0
    fi

    if grep -q "WP_REVERSE_PROXY_SSL" "$cfg" 2>/dev/null; then
        return 0
    fi

    local block_file
    block_file="$(mktemp)"
    cat > "$block_file" <<'EOF'

/* WP_REVERSE_PROXY_SSL */
if (
    (!empty($_SERVER['HTTP_X_FORWARDED_PROTO']) && strtolower(trim(explode(',', (string) $_SERVER['HTTP_X_FORWARDED_PROTO'])[0])) === 'https')
    || (!empty($_SERVER['HTTP_X_FORWARDED_SSL']) && strtolower((string) $_SERVER['HTTP_X_FORWARDED_SSL']) === 'on')
) {
    $_SERVER['HTTPS'] = 'on';
}
/* END WP_REVERSE_PROXY_SSL */

EOF

    if grep -q "wp-settings.php" "$cfg"; then
        awk -v bf="$block_file" '
            /wp-settings\.php/ && !done {
                while ((getline line < bf) > 0) print line
                close(bf)
                done=1
            }
            { print }
        ' "$cfg" > "${cfg}.tmp" && mv "${cfg}.tmp" "$cfg"
    else
        cat "$block_file" >> "$cfg"
    fi
    rm -f "$block_file"
    echo "[INIT] Applied reverse-proxy HTTPS detection for WordPress"
}

# Let WordPress write plugins/themes/uploads directly (no FTP prompt).
# FS_CHMOD_* keeps year/month upload dirs group-writable for www-data.
ensure_wp_filesystem_direct() {
    local cfg="/var/www/html/wp-config.php"
    if [ ! -f "$cfg" ]; then
        return 0
    fi

    local block_file
    local need_fs=0
    local need_chmod=0

    if ! grep -q "FS_METHOD" "$cfg" 2>/dev/null; then
        need_fs=1
    fi
    if ! grep -q "FS_CHMOD_DIR" "$cfg" 2>/dev/null; then
        need_chmod=1
    fi
    if [ "$need_fs" -eq 0 ] && [ "$need_chmod" -eq 0 ]; then
        return 0
    fi

    block_file="$(mktemp)"
    if [ "$need_fs" -eq 1 ] && [ "$need_chmod" -eq 1 ]; then
        cat > "$block_file" <<'EOF'

/* WP_FS_DIRECT */
define('FS_METHOD', 'direct');
define('FS_CHMOD_DIR', 0775);
define('FS_CHMOD_FILE', 0664);
/* END WP_FS_DIRECT */

EOF
    elif [ "$need_fs" -eq 1 ]; then
        cat > "$block_file" <<'EOF'

/* WP_FS_DIRECT */
define('FS_METHOD', 'direct');
/* END WP_FS_DIRECT */

EOF
    else
        cat > "$block_file" <<'EOF'

/* WP_FS_CHMOD */
define('FS_CHMOD_DIR', 0775);
define('FS_CHMOD_FILE', 0664);
/* END WP_FS_CHMOD */

EOF
    fi

    if grep -q "wp-settings.php" "$cfg"; then
        awk -v bf="$block_file" '
            /wp-settings\.php/ && !done {
                while ((getline line < bf) > 0) print line
                close(bf)
                done=1
            }
            { print }
        ' "$cfg" > "${cfg}.tmp" && mv "${cfg}.tmp" "$cfg"
    else
        cat "$block_file" >> "$cfg"
    fi
    rm -f "$block_file"
    echo "[INIT] Enabled direct filesystem writes for WordPress (FS_METHOD/FS_CHMOD)"
}

# PHP-FPM, panel installs, and FileBrowser must share www-data ownership
# so plugin/theme/upload installs never hit permission errors.
ensure_wordpress_permissions() {
    mkdir -p /var/www/html

    # PHP stores upload temp files here before move_uploaded_file into uploads/.
    mkdir -p /tmp/php-uploads
    chmod 1777 /tmp 2>/dev/null || true
    chown www-data:www-data /tmp/php-uploads 2>/dev/null || true
    chmod 1777 /tmp/php-uploads 2>/dev/null || true

    if [ -f /var/www/html/wp-load.php ] || [ -d /var/www/html/wp-includes ]; then
        mkdir -p \
            /var/www/html/wp-content \
            /var/www/html/wp-content/uploads \
            /var/www/html/wp-content/uploads/"$(date +%Y/%m)" \
            /var/www/html/wp-content/plugins \
            /var/www/html/wp-content/themes \
            /var/www/html/wp-content/upgrade \
            /var/www/html/wp-content/upgrade/temp \
            /var/www/html/wp-content/cache \
            /var/www/html/wp-content/mu-plugins
    fi

    chown -R www-data:www-data /var/www 2>/dev/null || true

    find /var/www/html -type d -exec chmod 755 {} + 2>/dev/null || true
    find /var/www/html -type f -exec chmod 644 {} + 2>/dev/null || true

    if [ -d /var/www/html/wp-content ]; then
        # setgid (2775) so new dirs/files keep www-data group for PHP + FileBrowser
        find /var/www/html/wp-content -type d -exec chmod 2775 {} + 2>/dev/null || true
        find /var/www/html/wp-content -type f -exec chmod 664 {} + 2>/dev/null || true
        chmod -R u+rwX,g+rwX \
            /var/www/html/wp-content/uploads \
            /var/www/html/wp-content/plugins \
            /var/www/html/wp-content/themes \
            /var/www/html/wp-content/upgrade \
            /var/www/html/wp-content/cache \
            2>/dev/null || true
    fi

    if [ -f /var/www/html/wp-config.php ]; then
        chown www-data:www-data /var/www/html/wp-config.php 2>/dev/null || true
        chmod 640 /var/www/html/wp-config.php 2>/dev/null || true
    fi

    echo "[INIT] Applied WordPress ownership and permissions under /var/www"
}

# Offload cron from page views (stops loopback storms into php-fpm).
# Supervisor runs due events every few minutes instead.
ensure_disable_wp_cron() {
    local cfg="/var/www/html/wp-config.php"
    if [ ! -f "$cfg" ]; then
        return 0
    fi

    if grep -q "DISABLE_WP_CRON" "$cfg" 2>/dev/null; then
        return 0
    fi

    local block_file
    block_file="$(mktemp)"
    cat > "$block_file" <<'EOF'

/* WP_CRON_OFFLOAD */
define('DISABLE_WP_CRON', true);
/* END WP_CRON_OFFLOAD */

EOF

    if grep -q "wp-settings.php" "$cfg"; then
        awk -v bf="$block_file" '
            /wp-settings\.php/ && !done {
                while ((getline line < bf) > 0) print line
                close(bf)
                done=1
            }
            { print }
        ' "$cfg" > "${cfg}.tmp" && mv "${cfg}.tmp" "$cfg"
    else
        cat "$block_file" >> "$cfg"
    fi
    rm -f "$block_file"
    echo "[INIT] Disabled WP-Cron on page load (supervisor handles due events)"
}

ensure_wordpress_htaccess
ensure_wp_performance_config
ensure_wp_reverse_proxy_ssl
ensure_wp_filesystem_direct
ensure_disable_wp_cron
ensure_wordpress_permissions

# =========================
# USE ENV (DEFAULTS ONLY IF NOT PROVIDED)
# =========================
: "${DB_NAME:=wordpress}"
: "${DB_USER:=wordpress}"
: "${DB_PASS:=123456}"
: "${DB_ROOT_PASS:=root123456}"
: "${FILEBROWSER_USER:=admin}"
: "${FILEBROWSER_PASS:=admin123@qwe}"

# Keep FileBrowser behind the configured public path (default /filebrowser).
# Root /var/www so the UI stays inside the web files tree.
FILE_DB="/database/filebrowser.db"
mkdir -p /database /config
FILEBROWSER_BASEURL="/filebrowser"
PHPMYADMIN_BASEURL="/phpmyadmin"
if [ -f /config/panel-paths.env ]; then
    # shellcheck disable=SC1091
    . /config/panel-paths.env
fi
: "${FILEBROWSER_BASEURL:=/filebrowser}"
: "${PHPMYADMIN_BASEURL:=/phpmyadmin}"
if [ -f "$FILE_DB" ]; then
    /usr/local/bin/filebrowser config set \
        --database "$FILE_DB" \
        --address 127.0.0.1 \
        --port 8080 \
        --baseurl "${FILEBROWSER_BASEURL}" \
        --root /var/www >/dev/null 2>&1 || true
    # Env password only applied on first DB create; re-sync so compose changes work.
    /usr/local/bin/filebrowser users update "${FILEBROWSER_USER}" \
        --password "${FILEBROWSER_PASS}" \
        --scope /var/www \
        --perm.admin \
        --database "$FILE_DB" >/dev/null 2>&1 || true
fi
chown www-data:www-data /database "$FILE_DB" 2>/dev/null || true
chmod 775 /database 2>/dev/null || true
chmod 664 "$FILE_DB" 2>/dev/null || true

# FileBrowser runs as www-data; PHP ini trees stay root-owned but writable and ensure
# apache2/fpm/cli layouts exist so paths like /etc/php/8.1/apache2/php.ini work.
ensure_php_ini_writable() {
    local ver sapi src dst ver_dir

    chmod 755 /etc/php 2>/dev/null || true

    for ver_dir in /etc/php/*; do
        [ -d "$ver_dir" ] || continue
        ver="$(basename "$ver_dir")"
        case "$ver" in
            [0-9]*.[0-9]*) ;;
            *) continue ;;
        esac

        for sapi in fpm cli apache2; do
            mkdir -p "/etc/php/${ver}/${sapi}/conf.d"
            dst="/etc/php/${ver}/${sapi}/php.ini"
            if [ ! -f "$dst" ]; then
                src=""
                for candidate in \
                    "/etc/php/${ver}/fpm/php.ini" \
                    "/etc/php/${ver}/cli/php.ini" \
                    "/usr/share/php${ver}-common/php.ini-production" \
                    "/usr/share/php${ver}/php.ini-production"; do
                    if [ -f "$candidate" ]; then
                        src="$candidate"
                        break
                    fi
                done
                if [ -n "$src" ]; then
                    cp "$src" "$dst"
                else
                    printf '; php.ini created for FileBrowser editing\n' > "$dst"
                fi
            fi
            chown root:root "$dst" 2>/dev/null || true
            chmod 664 "$dst" 2>/dev/null || true
        done

        find "/etc/php/${ver}" -type d -exec chmod 755 {} + 2>/dev/null || true
        find "/etc/php/${ver}" -type f -name '*.ini' -exec chmod 664 {} + 2>/dev/null || true
    done
}
ensure_php_ini_writable

# =========================
# FIRST RUN CHECK
# =========================
if [ -f "$STATE_FILE" ]; then
    # Permissions / FS_METHOD already applied above; keep permalinks fresh.
    flush_permalinks
    echo "[INIT] Already initialized. Skipping bootstrap..."
    exit 0
fi

sleep 10

# =========================
# MYSQL SETUP
# =========================
mysql -e "ALTER USER 'root'@'localhost' IDENTIFIED BY '${DB_ROOT_PASS}';"

mysql -uroot -p${DB_ROOT_PASS} -e "CREATE DATABASE IF NOT EXISTS ${DB_NAME};"

mysql -uroot -p${DB_ROOT_PASS} -e \
"CREATE USER IF NOT EXISTS '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASS}';"

mysql -uroot -p${DB_ROOT_PASS} -e \
"GRANT ALL PRIVILEGES ON ${DB_NAME}.* TO '${DB_USER}'@'localhost';"

mysql -uroot -p${DB_ROOT_PASS} -e "FLUSH PRIVILEGES;"

# =========================
# WORDPRESS CONFIG
# =========================
if [ -f /var/www/html/wp-config-sample.php ]; then
    cp /var/www/html/wp-config-sample.php /var/www/html/wp-config.php

    sed -i "s/database_name_here/${DB_NAME}/" /var/www/html/wp-config.php
    sed -i "s/username_here/${DB_USER}/" /var/www/html/wp-config.php
    sed -i "s/password_here/${DB_PASS}/" /var/www/html/wp-config.php
fi
ensure_wp_performance_config
ensure_wp_reverse_proxy_ssl

# Core is installed from /panel/wordpress — leave a hint when html is empty.
if [ ! -f /var/www/html/wp-load.php ]; then
    mkdir -p /var/www/html
    if [ ! -f /var/www/html/index.php ] && [ ! -f /var/www/html/index.html ]; then
        cat > /var/www/html/index.html <<'HTML'
<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>وردپرس نصب نشده</title></head>
<body style="font-family:sans-serif;padding:2rem;text-align:center">
<h1>وردپرس هنوز نصب نشده</h1>
<p>از <a href="/panel/wordpress">پنل مدیریت ← نصب وردپرس</a> نسخه را انتخاب و نصب کنید.</p>
</body>
</html>
HTML
        echo "[INIT] WordPress core missing — placeholder index written (install via /panel/wordpress)"
    fi
else
    rm -f /var/www/html/index.html || true
fi
ensure_wp_filesystem_direct
ensure_wordpress_permissions

# =========================
# MARK INSTALL DONE
# =========================
mkdir -p /var/lib
touch "$STATE_FILE"

# =========================
# FILEBROWSER INIT USER
# =========================

FILE_DB="/database/filebrowser.db"
mkdir -p /database /config
FILEBROWSER_BASEURL="/filebrowser"
PHPMYADMIN_BASEURL="/phpmyadmin"
if [ -f /config/panel-paths.env ]; then
    # shellcheck disable=SC1091
    . /config/panel-paths.env
fi
: "${FILEBROWSER_BASEURL:=/filebrowser}"
: "${PHPMYADMIN_BASEURL:=/phpmyadmin}"

if [ ! -f "$FILE_DB" ]; then
    /usr/local/bin/filebrowser config init \
        --database "$FILE_DB" \
        --address 127.0.0.1 \
        --port 8080 \
        --baseurl "${FILEBROWSER_BASEURL}" \
        --root /var/www

    /usr/local/bin/filebrowser users add \
        "${FILEBROWSER_USER}" \
        "${FILEBROWSER_PASS}" \
        --database "$FILE_DB" \
        --scope /var/www \
        --perm.admin || true
else
    /usr/local/bin/filebrowser users update "${FILEBROWSER_USER}" \
        --password "${FILEBROWSER_PASS}" \
        --scope /var/www \
        --perm.admin \
        --database "$FILE_DB" >/dev/null 2>&1 || \
    /usr/local/bin/filebrowser users add \
        "${FILEBROWSER_USER}" \
        "${FILEBROWSER_PASS}" \
        --database "$FILE_DB" \
        --scope /var/www \
        --perm.admin || true
fi
chown www-data:www-data /database "$FILE_DB" 2>/dev/null || true
chmod 775 /database 2>/dev/null || true
chmod 664 "$FILE_DB" 2>/dev/null || true

# =========================
# PHPMYADMIN CONFIG
# =========================
PMA_CONFIG="/usr/share/phpmyadmin/config.inc.php"
PMA_TMP="/var/lib/phpmyadmin/tmp"
mkdir -p "$PMA_TMP"
chown -R www-data:www-data /var/lib/phpmyadmin

BLOWFISH=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 32)

cat > "$PMA_CONFIG" <<EOF
<?php
declare(strict_types=1);

\$cfg['blowfish_secret'] = '${BLOWFISH}';
\$cfg['TempDir'] = '${PMA_TMP}';

\$i = 0;
\$i++;
\$cfg['Servers'][\$i]['auth_type'] = 'cookie';
\$cfg['Servers'][\$i]['host'] = 'localhost';
\$cfg['Servers'][\$i]['compress'] = false;
\$cfg['Servers'][\$i]['AllowNoPassword'] = false;
EOF

chown www-data:www-data "$PMA_CONFIG"
chmod 640 "$PMA_CONFIG"

# =========================
# OUTPUT (ONLY ON FIRST RUN)
# =========================
echo ""
echo "======================================"
echo " FIRST RUN SETUP COMPLETED"
echo "======================================"
echo "Domain       : ${PUBLIC_HOST:-localhost}"
echo "WordPress DB MariaDB"
echo "DB_NAME      : ${DB_NAME}"
echo "DB_USER      : ${DB_USER}"
echo "DB_PASSWORD  : ${DB_PASS}"
echo "DB_ROOT_PASS : ${DB_ROOT_PASS}"
echo ""
echo "Admin Panel (required before FileBrowser / phpMyAdmin)"
echo "URL      : http://${PUBLIC_HOST:-localhost}/panel"
echo "USERNAME : ${PANEL_USER:-admin}"
echo "PASSWORD : ${PANEL_PASS:-}"
echo ""
echo "FileBrowser"
echo "URL      : http://${PUBLIC_HOST:-localhost}${FILEBROWSER_BASEURL}"
echo "USERNAME : ${FILEBROWSER_USER}"
echo "PASSWORD : ${FILEBROWSER_PASS}"
echo ""
echo "phpMyAdmin"
echo "URL      : http://${PUBLIC_HOST:-localhost}${PHPMYADMIN_BASEURL}"
echo "LOGIN    : use DB_USER/DB_PASS or root / DB_ROOT_PASS"
echo "(tool paths can be changed later from /panel/settings)"
echo "======================================"
echo ""