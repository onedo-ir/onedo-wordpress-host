FROM gitea.tmayt.ir/thaiostream/ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# ===== Iranian mirror =====
RUN echo "deb http://mirror.arvancloud.ir/ubuntu/ jammy main restricted universe multiverse" > /etc/apt/sources.list \
 && echo "deb http://mirror.arvancloud.ir/ubuntu/ jammy-updates main restricted universe multiverse" >> /etc/apt/sources.list \
 && echo "deb http://mirror.arvancloud.ir/ubuntu/ jammy-backports main restricted universe multiverse" >> /etc/apt/sources.list \
 && echo "deb http://mirror.arvancloud.ir/ubuntu/ jammy-security main restricted universe multiverse" >> /etc/apt/sources.list

# ===== Install all services (multi PHP-FPM via ondrej; panel can switch) =====
ENV DEFAULT_PHP_VERSION=8.1
ENV PHP_VERSIONS="8.1 8.2 8.3 8.4"
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg software-properties-common \
    && add-apt-repository -y ppa:ondrej/php \
    && apt-get update && apt-get install -y --no-install-recommends \
    apache2 mariadb-server \
    php8.1-fpm php8.1-cli php8.1-mysql php8.1-curl php8.1-xml php8.1-mbstring php8.1-zip php8.1-gd php8.1-opcache \
    php8.2-fpm php8.2-cli php8.2-mysql php8.2-curl php8.2-xml php8.2-mbstring php8.2-zip php8.2-gd php8.2-opcache \
    php8.3-fpm php8.3-cli php8.3-mysql php8.3-curl php8.3-xml php8.3-mbstring php8.3-zip php8.3-gd php8.3-opcache \
    php8.4-fpm php8.4-cli php8.4-mysql php8.4-curl php8.4-xml php8.4-mbstring php8.4-zip php8.4-gd php8.4-opcache \
    python3 python3-flask python3-gunicorn \
    wget unzip tar openssl supervisor \
    && rm -rf /var/lib/apt/lists/*

# ===== Apache: event MPM + FPM + compression/cache =====
COPY apache/000-default.conf /etc/apache2/sites-available/000-default.conf
COPY performance/apache-mpm.conf /etc/apache2/conf-available/mpm-tuning.conf
COPY performance/apache-performance.conf /etc/apache2/conf-available/performance.conf
COPY performance/apache-php-fpm.conf /etc/apache2/conf-available/php-fpm.conf
COPY performance/apache-protect.conf /etc/apache2/conf-available/protect.conf
COPY panel/apache-panel.conf /etc/apache2/conf-available/panel.conf
RUN set -eux; \
    # PHP mod_* pins mpm_prefork — disable PHP SAPIs first, then switch MPM
    a2dismod php8.1 2>/dev/null || true; \
    a2dismod php8.2 2>/dev/null || true; \
    a2dismod php8.3 2>/dev/null || true; \
    a2dismod php8.4 2>/dev/null || true; \
    a2dismod mpm_prefork; \
    a2enmod mpm_event; \
    a2enmod rewrite proxy proxy_http proxy_fcgi setenvif headers deflate expires reqtimeout; \
    a2enconf mpm-tuning performance php-fpm protect panel; \
    # Placeholder gate (replaced at container start with real token)
    printf '%s\n' \
      'RewriteCond %{REQUEST_URI} ^/(filebrowser|phpmyadmin)(/|$) [NC]' \
      'RewriteRule ^ /panel/login?next=%{REQUEST_URI} [R=302,L,QSA]' \
      > /etc/apache2/panel-gate.rewrite; \
    sed -i '/<Directory \/var\/www\/>/,/<\/Directory>/ s/AllowOverride None/AllowOverride All/' /etc/apache2/apache2.conf; \
    a2ensite 000-default.conf; \
    sed -i 's|^ErrorLog .*|ErrorLog ${APACHE_LOG_DIR}/error.log|' /etc/apache2/apache2.conf

# ===== PHP performance + ionCube for every shipped version =====
# Tuning files live under /opt/php-tuning so runtime apt installs can re-apply them
COPY performance/php-performance.ini /opt/php-tuning/php-performance.ini
COPY performance/php-opcache.ini /opt/php-tuning/php-opcache.ini
COPY performance/php-fpm-pool.conf /opt/php-tuning/php-fpm-pool.conf
COPY performance/php-fpm-start.sh /usr/local/bin/php-fpm-start.sh
COPY performance/configure-php-versions.sh /usr/local/bin/configure-php-versions.sh
COPY ioncube /ioncube
RUN chmod +x /usr/local/bin/php-fpm-start.sh /usr/local/bin/configure-php-versions.sh \
 && /usr/local/bin/configure-php-versions.sh

# ===== MariaDB low-memory tuning =====
COPY performance/mariadb.cnf /etc/mysql/mariadb.conf.d/99-performance.cnf

# ===== Install phpMyAdmin =====
COPY phpmyadmin /phpmyadmin
RUN chmod +x /phpmyadmin/get.sh \
 && /phpmyadmin/get.sh \
 && cp /phpmyadmin/apache-phpmyadmin.conf /etc/apache2/conf-available/phpmyadmin.conf \
 && a2enconf phpmyadmin

# ===== Install FileBrowser (proxied at /filebrowser) =====
COPY filebrowser /filebrowser
RUN chmod +x /filebrowser/get.sh \
 && /filebrowser/get.sh \
 && cp /filebrowser/apache-filebrowser.conf /etc/apache2/conf-available/filebrowser.conf \
 && a2enconf filebrowser

# ===== WordPress docroot (core installed via /panel/wordpress) =====
COPY wordpress/.htaccess /opt/wordpress.htaccess
COPY wordpress/index.html /opt/wordpress-placeholder.html
RUN mkdir -p /var/www/html \
 && cp /opt/wordpress-placeholder.html /var/www/html/index.html \
 && chown -R www-data:www-data /var/www \
 && chmod 644 /opt/wordpress.htaccess /opt/wordpress-placeholder.html

# ===== Admin panel (/panel) =====
COPY panel /opt/panel
COPY panel/start.sh /usr/local/bin/panel-start.sh
RUN chmod +x /usr/local/bin/panel-start.sh /opt/panel/start.sh \
 && chmod -R a+rX /opt/panel

# ===== Supervisor config =====
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# ===== FileBrowser start wrapper =====
COPY filebrowser-start.sh /usr/local/bin/filebrowser-start.sh
RUN chmod +x /usr/local/bin/filebrowser-start.sh

# ===== Init script =====
COPY init.sh /init.sh
RUN chmod +x /init.sh

# ===== Container entry (writes panel gate, then supervisord) =====
COPY container-start.sh /usr/local/bin/container-start.sh
RUN chmod +x /usr/local/bin/container-start.sh

# ===== WP-CLI =====
RUN curl -O https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar \
 && chmod +x wp-cli.phar \
 && mv wp-cli.phar /usr/local/bin/wp

EXPOSE 80

CMD ["/usr/local/bin/container-start.sh"]
