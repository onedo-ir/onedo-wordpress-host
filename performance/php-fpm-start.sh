#!/bin/bash
set -euo pipefail

DEFAULT_PHP_VERSION="${DEFAULT_PHP_VERSION:-8.1}"
PHP_VER="${DEFAULT_PHP_VERSION}"

if [ -f /config/panel-paths.env ]; then
  # shellcheck disable=SC1091
  set -a
  # Only trust keys we own; ignore unrelated exports.
  # shellcheck disable=SC1090
  . /config/panel-paths.env
  set +a
  if [ -n "${PHP_VERSION:-}" ]; then
    PHP_VER="${PHP_VERSION}"
  fi
fi

if [ ! -x "/usr/sbin/php-fpm${PHP_VER}" ]; then
  echo "php-fpm${PHP_VER} missing; falling back to ${DEFAULT_PHP_VERSION}" >&2
  PHP_VER="${DEFAULT_PHP_VERSION}"
fi

if [ ! -x "/usr/sbin/php-fpm${PHP_VER}" ]; then
  echo "No usable php-fpm binary found" >&2
  exit 1
fi

mkdir -p /run/php /tmp/php-uploads
chmod 1777 /tmp /tmp/php-uploads
chown www-data:www-data /tmp/php-uploads
touch /var/log/php-fpm-slow.log
chown www-data:www-data /var/log/php-fpm-slow.log

exec "/usr/sbin/php-fpm${PHP_VER}" --nodaemonize --fpm-config "/etc/php/${PHP_VER}/fpm/php-fpm.conf"
