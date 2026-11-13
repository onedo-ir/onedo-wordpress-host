#!/bin/bash
# Configure every shipped PHP version: performance ini, FPM pool, ionCube, CLI alternatives.
set -euo pipefail

VERSIONS="${PHP_VERSIONS:-8.1 8.2 8.3 8.4}"
DEFAULT_PHP_VERSION="${DEFAULT_PHP_VERSION:-8.1}"

for ver in ${VERSIONS}; do
  echo "Configuring PHP ${ver}"
  mkdir -p \
    "/etc/php/${ver}/fpm/conf.d" \
    "/etc/php/${ver}/cli/conf.d" \
    "/etc/php/${ver}/apache2/conf.d" \
    "/etc/php/${ver}/fpm/pool.d"

  cp /tmp/php-performance.ini "/etc/php/${ver}/fpm/conf.d/99-performance.ini"
  cp /tmp/php-performance.ini "/etc/php/${ver}/cli/conf.d/99-performance.ini"
  cp /tmp/php-opcache.ini "/etc/php/${ver}/fpm/conf.d/10-opcache-tune.ini"
  cp /tmp/php-opcache.ini "/etc/php/${ver}/cli/conf.d/10-opcache-tune.ini"
  cp /tmp/php-fpm-pool.conf "/etc/php/${ver}/fpm/pool.d/www.conf"

  for sapi in fpm cli apache2; do
    dst="/etc/php/${ver}/${sapi}/php.ini"
    if [ ! -f "${dst}" ]; then
      if [ -f "/etc/php/${ver}/fpm/php.ini" ]; then
        cp "/etc/php/${ver}/fpm/php.ini" "${dst}"
      elif [ -f "/etc/php/${ver}/cli/php.ini" ]; then
        cp "/etc/php/${ver}/cli/php.ini" "${dst}"
      fi
    fi
  done

  find "/etc/php/${ver}" -type f -name '*.ini' -exec chmod 664 {} + 2>/dev/null || true
  find "/etc/php/${ver}" -type d -exec chmod 755 {} + 2>/dev/null || true

  if [ -x "/usr/bin/php${ver}" ]; then
    EXT_DIR="$(/usr/bin/php${ver} -i 2>/dev/null | awk -F'=> ' '/^extension_dir/ {print $2; exit}' | tr -d ' ')"
    if [ -n "${EXT_DIR}" ]; then
      mkdir -p "${EXT_DIR}"
      IONCUBE=""
      if [ -f "/ioncube/ioncube_loader_lin_${ver}.so" ]; then
        IONCUBE="/ioncube/ioncube_loader_lin_${ver}.so"
      elif [ -f "/ioncube/ioncube_loader_lin_${ver}_ts.so" ]; then
        IONCUBE="/ioncube/ioncube_loader_lin_${ver}_ts.so"
      fi
      if [ -n "${IONCUBE}" ]; then
        cp "${IONCUBE}" "${EXT_DIR}/"
        base="$(basename "${IONCUBE}")"
        for sapi in fpm cli apache2; do
          confdir="/etc/php/${ver}/${sapi}/conf.d"
          if [ -d "${confdir}" ]; then
            echo "zend_extension=${EXT_DIR}/${base}" > "${confdir}/00-ioncube.ini"
            chmod 664 "${confdir}/00-ioncube.ini"
          fi
        done
      else
        echo "WARN: no ionCube loader for PHP ${ver}"
      fi
    fi
  fi
done

mkdir -p /run/php

# Prefer the stack default via alternatives (panel may switch later).
prio=10
for ver in ${VERSIONS}; do
  if [ -x "/usr/bin/php${ver}" ]; then
    update-alternatives --install /usr/bin/php php "/usr/bin/php${ver}" "${prio}" || true
    prio=$((prio + 10))
  fi
done

if [ -x "/usr/bin/php${DEFAULT_PHP_VERSION}" ]; then
  update-alternatives --set php "/usr/bin/php${DEFAULT_PHP_VERSION}" || true
fi

echo "Default PHP CLI: ${DEFAULT_PHP_VERSION}"
