#!/bin/bash
# Configure every shipped PHP version: performance ini, FPM pool, ionCube, CLI alternatives.
set -euo pipefail

VERSIONS="${PHP_VERSIONS:-8.1 8.2 8.3 8.4}"
DEFAULT_PHP_VERSION="${DEFAULT_PHP_VERSION:-8.1}"

if [ -f /opt/php-tuning/php-performance.ini ]; then
  PHP_TUNING_DIR=/opt/php-tuning
elif [ -f /tmp/php-performance.ini ]; then
  PHP_TUNING_DIR=/tmp
else
  PHP_TUNING_DIR=/opt/php-tuning
fi

zend_api_dir() {
  case "$1" in
    8.1) echo /usr/lib/php/20210902 ;;
    8.2) echo /usr/lib/php/20220829 ;;
    8.3) echo /usr/lib/php/20230831 ;;
    8.4) echo /usr/lib/php/20240924 ;;
    8.5) echo /usr/lib/php/20250925 ;;
    *) echo "" ;;
  esac
}

resolve_extension_dir() {
  local ver="$1"
  local dir=""
  # -n: ignore php.ini / conf.d so tuned drop-ins cannot abort discovery
  if [ -x "/usr/bin/php${ver}" ]; then
    dir="$(/usr/bin/php${ver} -n -r 'echo ini_get("extension_dir");' 2>/dev/null || true)"
  fi
  if [ -z "${dir}" ]; then
    dir="$(zend_api_dir "${ver}")"
  fi
  if [ -n "${dir}" ] && [ -d "${dir}" ]; then
    echo "${dir}"
    return 0
  fi
  echo ""
  return 0
}

mkdir -p /tmp/php-uploads /run/php
chmod 1777 /tmp/php-uploads 2>/dev/null || true

for ver in ${VERSIONS}; do
  echo "Configuring PHP ${ver}"

  if [ ! -x "/usr/bin/php${ver}" ]; then
    echo "WARN: /usr/bin/php${ver} missing, skipping"
    continue
  fi

  mkdir -p \
    "/etc/php/${ver}/fpm/conf.d" \
    "/etc/php/${ver}/cli/conf.d" \
    "/etc/php/${ver}/apache2/conf.d" \
    "/etc/php/${ver}/fpm/pool.d"

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

  EXT_DIR="$(resolve_extension_dir "${ver}")"
  if [ -n "${EXT_DIR}" ]; then
    echo "  extension_dir=${EXT_DIR}"
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
          # 00- so ionCube loads before OPcache
          echo "zend_extension=${EXT_DIR}/${base}" > "${confdir}/00-ioncube.ini"
          chmod 664 "${confdir}/00-ioncube.ini"
        fi
      done
      echo "  ionCube: ${base}"
    else
      echo "WARN: no ionCube loader for PHP ${ver}"
    fi
  else
    echo "WARN: could not resolve extension_dir for PHP ${ver}; skipping ionCube"
  fi

  # Drop-ins after ionCube so a bad tune cannot block loader install during build
  if [ ! -f "${PHP_TUNING_DIR}/php-performance.ini" ] \
    || [ ! -f "${PHP_TUNING_DIR}/php-opcache.ini" ] \
    || [ ! -f "${PHP_TUNING_DIR}/php-fpm-pool.conf" ]; then
    echo "WARN: PHP tuning files missing under ${PHP_TUNING_DIR}; skipping drop-ins for ${ver}"
  else
    cp "${PHP_TUNING_DIR}/php-performance.ini" "/etc/php/${ver}/fpm/conf.d/99-performance.ini"
    cp "${PHP_TUNING_DIR}/php-performance.ini" "/etc/php/${ver}/cli/conf.d/99-performance.ini"
    cp "${PHP_TUNING_DIR}/php-opcache.ini" "/etc/php/${ver}/fpm/conf.d/10-opcache-tune.ini"
    cp "${PHP_TUNING_DIR}/php-opcache.ini" "/etc/php/${ver}/cli/conf.d/10-opcache-tune.ini"
    cp "${PHP_TUNING_DIR}/php-fpm-pool.conf" "/etc/php/${ver}/fpm/pool.d/www.conf"
  fi

  find "/etc/php/${ver}" -type f -name '*.ini' -exec chmod 664 {} + 2>/dev/null || true
  find "/etc/php/${ver}" -type d -exec chmod 755 {} + 2>/dev/null || true

  echo "  PHP ${ver} configured"
done

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
