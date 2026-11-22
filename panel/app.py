#!/usr/bin/env python3
"""Admin panel at /panel — login, config edit, shell, backup, links to tools."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from functools import wraps
from glob import glob
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from backup_schedule import (
    WEEKDAY_LABELS,
    assess_backup_storage_risk,
    create_stored_db_backup,
    create_stored_full_backup,
    delete_stored_backup,
    load_backup_settings,
    list_stored_backups,
    parse_backup_settings_form,
    resolve_stored_backup,
    restore_stored_db,
    restore_stored_full,
    save_backup_settings,
)
from agent import (
    MAX_HISTORY_MESSAGES,
    AgentCancelled,
    _normalize_history,
    agent_configured,
    run_agent_turn,
)

app = Flask(
    __name__,
    static_url_path="/panel/static",
    static_folder="static",
    template_folder="templates",
)
app.config["APPLICATION_ROOT"] = "/panel"
app.secret_key = os.environ.get("PANEL_SECRET") or os.environ.get(
    "PANEL_PASS", "wp-panel-change-me"
)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 12
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GiB restore uploads

COOKIE_NAME = "panel_gate"
SHELL_TIMEOUT = 45
SHELL_MAX_OUTPUT = 200_000
WP_ROOT = Path("/var/www/html")
WP_HTACCESS_SRC = Path("/opt/wordpress.htaccess")
WP_VERSIONS_API = "https://api.wordpress.org/core/stable-check/1.0/"
WP_INSTALL_TIMEOUT = 600
WP_VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,3}$")
BACKUP_WORKDIR = Path("/tmp/panel-backups")
BACKUP_CREATE_TIMEOUT = 600
BACKUP_RESTORE_TIMEOUT = 900

PANEL_SETTINGS_PATH = Path("/config/panel-settings.json")
PANEL_PATHS_ENV = Path("/config/panel-paths.env")
CHAT_STORE_PATH = Path("/config/panel-chats.json")
LOGIN_LOCKOUT_PATH = Path("/config/panel-login-lockout.json")
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 60 * 60
_login_lockout_lock = threading.Lock()
DEFAULT_FILEBROWSER_PATH = "/filebrowser"
DEFAULT_PHPMYADMIN_PATH = "/phpmyadmin"
DEFAULT_PHP_VERSION = os.environ.get("DEFAULT_PHP_VERSION", "8.1").strip() or "8.1"
PHP_EXT_SUFFIXES = (
    "fpm",
    "cli",
    "mysql",
    "curl",
    "xml",
    "mbstring",
    "zip",
    "gd",
    "opcache",
)
PHP_INSTALL_TIMEOUT = 900
PHP_VERSIONS_CACHE_TTL = 600
PHP_FPM_PKG_RE = re.compile(r"^php(\d+\.\d+)-fpm$")
_apt_lock = threading.Lock()
_php_versions_cache: dict[str, object] = {"at": 0.0, "items": []}
PUBLIC_PATH_RE = re.compile(r"^/[A-Za-z0-9](?:[A-Za-z0-9_-]*/?[A-Za-z0-9_-]+)*$")
RESERVED_PUBLIC_PATHS = {
    "/panel",
    "/index.php",
    "/wp-admin",
    "/wp-content",
    "/wp-includes",
    "/wp-json",
    "/xmlrpc.php",
}
FILEBROWSER_APACHE_CONF = Path("/etc/apache2/conf-available/filebrowser.conf")
PHPMYADMIN_APACHE_CONF = Path("/etc/apache2/conf-available/phpmyadmin.conf")
PANEL_GATE_REWRITE = Path("/etc/apache2/panel-gate.rewrite")
PANEL_TOOL_PATHS_REWRITE = Path("/etc/apache2/panel-tool-paths.rewrite")
FILEBROWSER_DB = Path("/database/filebrowser.db")


def panel_user() -> str:
    return os.environ.get("PANEL_USER", "admin")


def panel_pass() -> str:
    return os.environ.get("PANEL_PASS", "")


def secure_str_eq(a: str, b: str) -> bool:
    """Constant-time string compare; supports non-ASCII (hmac.compare_digest on str does not)."""
    ba = a.encode("utf-8")
    bb = b.encode("utf-8")
    if len(ba) != len(bb):
        hmac.compare_digest(ba, ba)
        return False
    return hmac.compare_digest(ba, bb)


def client_ip() -> str:
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if forwarded:
        return forwarded[:64]
    return (request.remote_addr or "unknown")[:64]


def _empty_lockout_entry() -> dict:
    return {"failures": 0, "locked_until": 0, "updated_at": 0}


def _load_login_lockout() -> dict:
    try:
        if LOGIN_LOCKOUT_PATH.is_file():
            data = json.loads(LOGIN_LOCKOUT_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return {}


def _save_login_lockout(data: dict) -> None:
    LOGIN_LOCKOUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LOGIN_LOCKOUT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(LOGIN_LOCKOUT_PATH)


def _prune_login_lockout(data: dict, now: float) -> dict:
    pruned: dict = {}
    for ip, raw in data.items():
        if not isinstance(ip, str) or not isinstance(raw, dict):
            continue
        try:
            failures = int(raw.get("failures") or 0)
            locked_until = float(raw.get("locked_until") or 0)
            updated_at = float(raw.get("updated_at") or 0)
        except (TypeError, ValueError):
            continue
        if locked_until > now:
            pruned[ip] = {
                "failures": max(failures, 0),
                "locked_until": locked_until,
                "updated_at": updated_at or now,
            }
            continue
        if failures > 0 and updated_at and (now - updated_at) < LOGIN_LOCKOUT_SECONDS:
            pruned[ip] = {
                "failures": failures,
                "locked_until": 0,
                "updated_at": updated_at,
            }
    return pruned


def login_lockout_remaining(ip: str) -> int:
    """Seconds left in lockout for this IP, or 0 if not locked."""
    now = time.time()
    with _login_lockout_lock:
        data = _prune_login_lockout(_load_login_lockout(), now)
        entry = data.get(ip) or _empty_lockout_entry()
        locked_until = float(entry.get("locked_until") or 0)
        remaining = int(locked_until - now)
        if remaining > 0:
            return remaining
        return 0


def record_login_failure(ip: str) -> int:
    """Record a failed login. Returns remaining lockout seconds (0 if not locked yet)."""
    now = time.time()
    with _login_lockout_lock:
        data = _prune_login_lockout(_load_login_lockout(), now)
        entry = dict(data.get(ip) or _empty_lockout_entry())
        locked_until = float(entry.get("locked_until") or 0)
        if locked_until > now:
            remaining = int(locked_until - now)
            _save_login_lockout(data)
            return max(remaining, 1)

        failures = int(entry.get("failures") or 0) + 1
        entry["failures"] = failures
        entry["updated_at"] = now
        if failures >= LOGIN_MAX_FAILURES:
            entry["locked_until"] = now + LOGIN_LOCKOUT_SECONDS
            entry["failures"] = 0
            remaining = LOGIN_LOCKOUT_SECONDS
        else:
            entry["locked_until"] = 0
            remaining = 0
        data[ip] = entry
        _save_login_lockout(data)
        return remaining


def clear_login_failures(ip: str) -> None:
    now = time.time()
    with _login_lockout_lock:
        data = _prune_login_lockout(_load_login_lockout(), now)
        if ip in data:
            data.pop(ip, None)
            _save_login_lockout(data)


def login_lockout_message(remaining_seconds: int) -> str:
    minutes = max(1, (remaining_seconds + 59) // 60)
    return (
        f"به دلیل ۵ تلاش ناموفق، ورود برای حدود {minutes} دقیقه مسدود است."
    )


def gate_token() -> str:
    """Cookie value Apache checks before allowing tool paths (FileBrowser / phpMyAdmin)."""
    explicit = os.environ.get("PANEL_GATE_TOKEN", "").strip()
    if explicit:
        return explicit
    raw = f"{panel_user()}:{panel_pass()}:{os.environ.get('PANEL_SECRET', 'wp-panel-gate')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def default_panel_settings() -> dict[str, str]:
    return {
        "filebrowser_path": DEFAULT_FILEBROWSER_PATH,
        "phpmyadmin_path": DEFAULT_PHPMYADMIN_PATH,
        "php_version": DEFAULT_PHP_VERSION,
    }


def normalize_public_path(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if not value.startswith("/"):
        value = "/" + value
    value = re.sub(r"/{2,}", "/", value)
    if len(value) > 1:
        value = value.rstrip("/")
    return value


def validate_public_path(path: str, *, label: str) -> str | None:
    if not path or path == "/":
        return f"مسیر {label} نمی‌تواند خالی یا ریشه باشد."
    if not PUBLIC_PATH_RE.fullmatch(path):
        return (
            f"مسیر {label} فقط می‌تواند شامل حروف، عدد، - و _ باشد "
            f"(مثل /filebrowser یا /pma)."
        )
    lowered = path.lower()
    if lowered in RESERVED_PUBLIC_PATHS or lowered.startswith("/panel/"):
        return f"مسیر {label} رزرو شده است و قابل استفاده نیست."
    return None


def normalize_php_version(raw: str) -> str:
    value = (raw or "").strip()
    if re.fullmatch(r"\d+\.\d+", value):
        return value
    return ""


def installed_php_versions() -> list[str]:
    """Discover installed PHP-FPM binaries on disk (not limited to image allowlist)."""
    found: list[str] = []
    sbin = Path("/usr/sbin")
    if sbin.is_dir():
        for path in sbin.glob("php-fpm*"):
            match = re.fullmatch(r"php-fpm(\d+\.\d+)", path.name)
            if match and path.is_file():
                found.append(match.group(1))
    found = sorted(set(found), key=_version_sort_key)
    return found or [DEFAULT_PHP_VERSION]


def validate_php_version(version: str) -> str | None:
    if not version:
        return "نسخه PHP را انتخاب کنید."
    ver = normalize_php_version(version)
    if not ver:
        return f"نسخه PHP {version} نامعتبر است."
    if not Path(f"/usr/sbin/php-fpm{ver}").is_file():
        return f"PHP {ver} روی این کانتینر نصب نیست."
    return None


def runtime_php_version() -> str:
    try:
        proc = subprocess.run(
            ["php", "-r", "echo PHP_MAJOR_VERSION.'.'.PHP_MINOR_VERSION;"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        ver = normalize_php_version((proc.stdout or "").strip())
        if ver:
            return ver
    except (OSError, subprocess.SubprocessError):
        pass
    return DEFAULT_PHP_VERSION


def load_panel_settings() -> dict[str, str]:
    settings = default_panel_settings()
    try:
        if PANEL_SETTINGS_PATH.is_file():
            data = json.loads(PANEL_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                fb = normalize_public_path(str(data.get("filebrowser_path") or ""))
                pma = normalize_public_path(str(data.get("phpmyadmin_path") or ""))
                php_ver = normalize_php_version(str(data.get("php_version") or ""))
                if fb and validate_public_path(fb, label="FileBrowser") is None:
                    settings["filebrowser_path"] = fb
                if pma and validate_public_path(pma, label="phpMyAdmin") is None:
                    settings["phpmyadmin_path"] = pma
                if php_ver and validate_php_version(php_ver) is None:
                    settings["php_version"] = php_ver
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return settings


def save_panel_settings(settings: dict[str, str]) -> None:
    PANEL_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    merged = load_panel_settings()
    for key in ("filebrowser_path", "phpmyadmin_path", "php_version"):
        value = settings.get(key)
        if value:
            merged[key] = value
    payload = {
        "filebrowser_path": merged["filebrowser_path"],
        "phpmyadmin_path": merged["phpmyadmin_path"],
        "php_version": merged["php_version"],
    }
    PANEL_SETTINGS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    PANEL_PATHS_ENV.write_text(
        (
            f"FILEBROWSER_BASEURL={payload['filebrowser_path']}\n"
            f"PHPMYADMIN_BASEURL={payload['phpmyadmin_path']}\n"
            f"PHP_VERSION={payload['php_version']}\n"
        ),
        encoding="utf-8",
    )


def _apache_regex_escape(path: str) -> str:
    return re.sub(r"([.^$|?*+(){}\\[\\]])", r"\\\1", path)


def write_panel_gate_rewrite(
    filebrowser_path: str | None = None,
    phpmyadmin_path: str | None = None,
) -> None:
    settings = load_panel_settings()
    fb = filebrowser_path or settings["filebrowser_path"]
    pma = phpmyadmin_path or settings["phpmyadmin_path"]
    fb_re = _apache_regex_escape(fb)
    pma_re = _apache_regex_escape(pma)
    token = gate_token()
    PANEL_GATE_REWRITE.write_text(
        (
            "# Auto-generated — requires panel login cookie before tool paths\n"
            f"RewriteCond %{{REQUEST_URI}} ^({fb_re}|{pma_re})(/|$) [NC]\n"
            f"RewriteCond %{{HTTP_COOKIE}} !{COOKIE_NAME}={token}\n"
            "RewriteRule ^ /panel/login?next=%{REQUEST_URI} [R=302,L,QSA]\n"
        ),
        encoding="utf-8",
    )


def write_tool_paths_rewrite(filebrowser_path: str, phpmyadmin_path: str) -> None:
    fb_re = _apache_regex_escape(filebrowser_path)
    pma_re = _apache_regex_escape(phpmyadmin_path)
    PANEL_TOOL_PATHS_REWRITE.write_text(
        (
            "# Auto-generated — skip WordPress front controller for tool paths\n"
            f"RewriteRule ^{pma_re} - [L]\n"
            f"RewriteRule ^{fb_re} - [L]\n"
        ),
        encoding="utf-8",
    )


def write_filebrowser_apache_conf(path: str) -> None:
    FILEBROWSER_APACHE_CONF.write_text(
        (
            "# Reverse-proxy FileBrowser on a path (like phpMyAdmin).\n"
            "# FileBrowser itself listens only on 127.0.0.1:8080 inside the container.\n"
            "# Regenerated by the admin panel settings.\n"
            "ProxyPreserveHost On\n"
            "ProxyRequests Off\n"
            "\n"
            f"<Location {path}>\n"
            f"    ProxyPass http://127.0.0.1:8080{path}\n"
            f"    ProxyPassReverse http://127.0.0.1:8080{path}\n"
            "    Require all granted\n"
            "</Location>\n"
        ),
        encoding="utf-8",
    )


def write_phpmyadmin_apache_conf(path: str) -> None:
    PHPMYADMIN_APACHE_CONF.write_text(
        (
            f"Alias {path} /usr/share/phpmyadmin\n"
            "\n"
            "<Directory /usr/share/phpmyadmin>\n"
            "    Options SymLinksIfOwnerMatch\n"
            "    DirectoryIndex index.php\n"
            "    AllowOverride All\n"
            "    Require all granted\n"
            "</Directory>\n"
            "\n"
            "<Directory /usr/share/phpmyadmin/templates>\n"
            "    Require all denied\n"
            "</Directory>\n"
            "<Directory /usr/share/phpmyadmin/libraries>\n"
            "    Require all denied\n"
            "</Directory>\n"
        ),
        encoding="utf-8",
    )


def sync_filebrowser_baseurl(path: str) -> None:
    if not FILEBROWSER_DB.is_file():
        return
    subprocess.run(
        [
            "/usr/local/bin/filebrowser",
            "config",
            "set",
            "--database",
            str(FILEBROWSER_DB),
            "--address",
            "127.0.0.1",
            "--port",
            "8080",
            "--baseurl",
            path,
            "--root",
            "/var/www",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def apply_php_cli_alternatives(version: str) -> None:
    php_bin = f"/usr/bin/php{version}"
    if not Path(php_bin).is_file():
        return
    subprocess.run(
        ["update-alternatives", "--install", "/usr/bin/php", "php", php_bin, "100"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    subprocess.run(
        ["update-alternatives", "--set", "php", php_bin],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def ensure_php_version_applied(
    version: str | None = None,
    *,
    restart_fpm: bool = False,
) -> str:
    """Persist selected PHP version, point CLI at it, optionally restart FPM."""
    current = load_panel_settings()
    ver = normalize_php_version(version or current.get("php_version") or "")
    if validate_php_version(ver) is not None:
        # Fall back to an installed binary (preference may be temporarily unavailable).
        ver = installed_php_versions()[0]
    save_panel_settings(
        {
            "filebrowser_path": current["filebrowser_path"],
            "phpmyadmin_path": current["phpmyadmin_path"],
            "php_version": ver,
        }
    )
    apply_php_cli_alternatives(ver)
    if restart_fpm:
        subprocess.run(
            ["supervisorctl", "restart", "php-fpm"],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    return ver


def _apt_env() -> dict[str, str]:
    return {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}


def _apt_get_update(*, timeout: int = 180) -> None:
    proc = subprocess.run(
        ["apt-get", "update"],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_apt_env(),
    )
    if proc.returncode != 0:
        err = ((proc.stderr or "") + (proc.stdout or "")).strip()
        raise RuntimeError(err or "apt-get update ناموفق بود.")


def _apt_cache_policy(package: str) -> tuple[str, str]:
    """Return (installed, candidate) package versions from apt-cache policy."""
    proc = subprocess.run(
        ["apt-cache", "policy", package],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=_apt_env(),
    )
    text = (proc.stdout or "") + (proc.stderr or "")
    installed = ""
    candidate = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Installed:"):
            value = stripped.split(":", 1)[1].strip()
            if value and value != "(none)":
                installed = value
        elif stripped.startswith("Candidate:"):
            value = stripped.split(":", 1)[1].strip()
            if value and value != "(none)":
                candidate = value
    return installed, candidate


def _php_packages_for_version(ver: str) -> tuple[list[str], list[str]]:
    """Return (installable, missing) package names for the WordPress PHP set.

    Some apt versions (e.g. early php8.6) publish -fpm while extensions like
    opcache are not in the repo yet — those must not be offered for install.
    """
    installable: list[str] = []
    missing: list[str] = []
    for suffix in PHP_EXT_SUFFIXES:
        pkg = f"php{ver}-{suffix}"
        installed, candidate = _apt_cache_policy(pkg)
        if installed or candidate:
            installable.append(pkg)
        else:
            missing.append(pkg)
    return installable, missing


def _annotate_php_version_items(
    raw_items: list[dict[str, str | bool]],
    active: str,
) -> list[dict[str, str | bool]]:
    """Refresh binary/active/status fields from cached apt package metadata."""
    items: list[dict[str, str | bool]] = []
    for raw in raw_items:
        ver = str(raw.get("version") or "")
        if not ver:
            continue
        installed_pkg = str(raw.get("installed_version") or "")
        candidate_pkg = str(raw.get("candidate_version") or "")
        has_binary = Path(f"/usr/sbin/php-fpm{ver}").is_file()
        is_active = has_binary and ver == active
        pkg_installed = bool(installed_pkg) or has_binary
        has_update = bool(
            pkg_installed
            and candidate_pkg
            and installed_pkg
            and candidate_pkg != installed_pkg
        )
        if is_active:
            status = "active"
        elif has_update:
            status = "update"
        elif pkg_installed:
            status = "installed"
        else:
            status = "available"
        items.append(
            {
                "version": ver,
                "status": status,
                "installed_version": installed_pkg,
                "candidate_version": candidate_pkg,
                "has_binary": has_binary,
                "is_active": is_active,
                "is_latest": False,
            }
        )
    if items:
        items[0]["is_latest"] = True
    return items


def fetch_php_versions(*, force_refresh: bool = False) -> list[dict[str, str | bool]]:
    """Discover PHP X.Y packages from apt (ondrej/php); cache apt metadata briefly."""
    active = normalize_php_version(
        load_panel_settings().get("php_version") or ""
    ) or DEFAULT_PHP_VERSION
    cached_items = _php_versions_cache.get("items") or []
    cached_at = float(_php_versions_cache.get("at") or 0.0)
    if (
        not force_refresh
        and isinstance(cached_items, list)
        and cached_items
        and (time.time() - cached_at) < PHP_VERSIONS_CACHE_TTL
    ):
        return _annotate_php_version_items(
            [dict(x) for x in cached_items if isinstance(x, dict)],
            active,
        )

    with _apt_lock:
        # Re-check cache inside the lock (another thread may have refreshed).
        cached_items = _php_versions_cache.get("items") or []
        cached_at = float(_php_versions_cache.get("at") or 0.0)
        if (
            not force_refresh
            and isinstance(cached_items, list)
            and cached_items
            and (time.time() - cached_at) < PHP_VERSIONS_CACHE_TTL
        ):
            return _annotate_php_version_items(
                [dict(x) for x in cached_items if isinstance(x, dict)],
                active,
            )

        _apt_get_update()
        proc = subprocess.run(
            ["apt-cache", "search", "--names-only", r"^php[0-9]+\.[0-9]+-fpm$"],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env=_apt_env(),
        )
        if proc.returncode != 0:
            err = ((proc.stderr or "") + (proc.stdout or "")).strip()
            raise RuntimeError(err or "apt-cache search ناموفق بود.")
        versions: set[str] = set()
        for line in (proc.stdout or "").splitlines():
            pkg = line.split(None, 1)[0].strip() if line.strip() else ""
            match = PHP_FPM_PKG_RE.match(pkg)
            if match:
                versions.add(match.group(1))
        if not versions:
            # Fallback: scan pkgnames when search yields nothing
            proc2 = subprocess.run(
                ["apt-cache", "pkgnames", "php"],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
                env=_apt_env(),
            )
            for line in (proc2.stdout or "").splitlines():
                match = PHP_FPM_PKG_RE.match(line.strip())
                if match:
                    versions.add(match.group(1))
        raw_items: list[dict[str, str | bool]] = []
        for ver in sorted(versions, key=_version_sort_key, reverse=True):
            pkg = f"php{ver}-fpm"
            installed_pkg, candidate_pkg = _apt_cache_policy(pkg)
            has_binary = Path(f"/usr/sbin/php-fpm{ver}").is_file()
            _installable, missing = _php_packages_for_version(ver)
            # Skip incomplete remote sets (e.g. php8.6-fpm without php8.6-opcache)
            # unless this version is already present on disk.
            if missing and not has_binary and not installed_pkg:
                continue
            raw_items.append(
                {
                    "version": ver,
                    "installed_version": installed_pkg,
                    "candidate_version": candidate_pkg,
                }
            )
        _php_versions_cache["items"] = raw_items
        _php_versions_cache["at"] = time.time()

    return _annotate_php_version_items(raw_items, active)


def install_or_update_php(version: str) -> str:
    """apt-install a PHP X.Y package set, then apply ionCube + performance drop-ins."""
    ver = normalize_php_version(version)
    if not ver:
        raise RuntimeError("نسخه PHP نامعتبر است.")
    active_before = normalize_php_version(
        load_panel_settings().get("php_version") or ""
    ) or DEFAULT_PHP_VERSION
    with _apt_lock:
        _apt_get_update(timeout=180)
        packages, missing = _php_packages_for_version(ver)
        if missing:
            # Core set incomplete in apt (seen with early php8.6-* packages).
            raise RuntimeError(
                f"بسته‌های لازم برای PHP {ver} در apt موجود نیست: "
                + ", ".join(missing)
            )
        if not packages:
            raise RuntimeError(f"هیچ بسته‌ای برای PHP {ver} در apt پیدا نشد.")
        proc = subprocess.run(
            [
                "apt-get",
                "install",
                "-y",
                "--no-install-recommends",
                *packages,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=PHP_INSTALL_TIMEOUT,
            env=_apt_env(),
        )
        if proc.returncode != 0:
            err = ((proc.stderr or "") + (proc.stdout or "")).strip()
            raise RuntimeError(err or f"نصب PHP {ver} ناموفق بود.")
        # Keep Apache on event MPM + FPM; disable mod_php if apt pulled it in
        subprocess.run(
            ["a2dismod", f"php{ver}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Prefer current panel PHP for CLI alternatives so configure does not reset it
        configure = subprocess.run(
            ["/usr/local/bin/configure-php-versions.sh"],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
            env={
                **os.environ,
                "PHP_VERSIONS": ver,
                "DEFAULT_PHP_VERSION": active_before,
            },
        )
        if configure.returncode != 0:
            err = ((configure.stderr or "") + (configure.stdout or "")).strip()
            raise RuntimeError(
                err or f"پیکربندی PHP {ver} (ionCube/performance) ناموفق بود."
            )
        # Force next list view to re-read apt policy for this version
        _php_versions_cache["at"] = 0.0
        _php_versions_cache["items"] = []
    if not Path(f"/usr/sbin/php-fpm{ver}").is_file():
        raise RuntimeError(f"پس از نصب، باینری php-fpm{ver} پیدا نشد.")
    if active_before == ver:
        ensure_php_version_applied(ver, restart_fpm=True)
        return f"PHP {ver} به‌روز و FPM ری‌استارت شد."
    detail = ((proc.stdout or "").strip().splitlines() or [""])[-1]
    return detail or f"PHP {ver} نصب شد."


def ensure_tool_paths_applied(
    settings: dict[str, str] | None = None,
    *,
    restart_filebrowser: bool = False,
    reload_apache: bool = False,
) -> dict[str, str]:
    """Persist and apply FileBrowser / phpMyAdmin public URL paths."""
    current = dict(settings or load_panel_settings())
    fb = current["filebrowser_path"]
    pma = current["phpmyadmin_path"]
    php_ver = current.get("php_version") or load_panel_settings()["php_version"]
    save_panel_settings(
        {
            "filebrowser_path": fb,
            "phpmyadmin_path": pma,
            "php_version": php_ver,
        }
    )
    write_filebrowser_apache_conf(fb)
    write_phpmyadmin_apache_conf(pma)
    write_tool_paths_rewrite(fb, pma)
    write_panel_gate_rewrite(fb, pma)
    sync_filebrowser_baseurl(fb)

    if reload_apache:
        subprocess.run(
            ["apache2ctl", "graceful"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    if restart_filebrowser:
        subprocess.run(
            ["supervisorctl", "restart", "filebrowser"],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    return load_panel_settings()


def allowed_config_files() -> list[str]:
    paths: list[str] = [
        "/etc/apache2/sites-available/000-default.conf",
        "/etc/apache2/conf-available/performance.conf",
        "/etc/apache2/conf-available/php-fpm.conf",
        "/etc/apache2/conf-available/mpm-tuning.conf",
        "/etc/apache2/conf-available/protect.conf",
        "/etc/apache2/conf-available/filebrowser.conf",
        "/etc/apache2/conf-available/phpmyadmin.conf",
        "/etc/mysql/mariadb.conf.d/99-performance.cnf",
        "/var/www/html/.htaccess",
        "/var/www/html/wp-config.php",
    ]
    for pattern in (
        "/etc/php/*/fpm/php.ini",
        "/etc/php/*/fpm/pool.d/www.conf",
        "/etc/php/*/fpm/conf.d/99-performance.ini",
        "/etc/php/*/fpm/conf.d/10-opcache-tune.ini",
        "/etc/php/*/cli/conf.d/99-performance.ini",
    ):
        paths.extend(sorted(glob(pattern)))
    # Unique, existing only
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        rp = str(Path(p).resolve()) if os.path.exists(p) else p
        if rp in seen:
            continue
        if os.path.isfile(p):
            seen.add(rp)
            out.append(p)
    return out


def resolve_allowed(path: str) -> str | None:
    if not path or ".." in path:
        return None
    allowed = {str(Path(p).resolve()): p for p in allowed_config_files()}
    try:
        rp = str(Path(path).resolve())
    except OSError:
        return None
    return allowed.get(rp)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("panel_ok"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def set_gate_cookie(resp):
    resp.set_cookie(
        COOKIE_NAME,
        gate_token(),
        httponly=True,
        samesite="Lax",
        path="/",
        max_age=60 * 60 * 12,
    )
    return resp


def clear_gate_cookie(resp):
    resp.set_cookie(COOKIE_NAME, "", expires=0, path="/")
    return resp


def db_settings() -> dict[str, str]:
    return {
        "name": os.environ.get("DB_NAME", "wordpress"),
        "user": os.environ.get("DB_USER", "wordpress"),
        "pass": os.environ.get("DB_PASS", ""),
        "root_pass": os.environ.get("DB_ROOT_PASS", ""),
    }


def _read_meminfo() -> dict[str, int]:
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0].endswith(":"):
                    key = parts[0][:-1]
                    try:
                        info[key] = int(parts[1])
                    except ValueError:
                        continue
    except OSError:
        pass
    return info


def _cpu_times() -> tuple[int, int] | None:
    try:
        with open("/proc/stat", encoding="utf-8") as fh:
            parts = fh.readline().split()
        if parts[0] != "cpu" or len(parts) < 5:
            return None
        nums = [int(x) for x in parts[1:]]
        total = sum(nums)
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        return total, idle
    except (OSError, ValueError, IndexError):
        return None


def _cpu_percent(sample_ms: int = 120) -> float | None:
    a = _cpu_times()
    if not a:
        return None
    time.sleep(sample_ms / 1000.0)
    b = _cpu_times()
    if not b:
        return None
    dt, di = b[0] - a[0], b[1] - a[1]
    if dt <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (1.0 - di / dt) * 100.0)), 1)


def _loadavg() -> list[float]:
    try:
        with open("/proc/loadavg", encoding="utf-8") as fh:
            parts = fh.read().split()
        return [float(parts[0]), float(parts[1]), float(parts[2])]
    except (OSError, ValueError, IndexError):
        return []


def _uptime_seconds() -> float | None:
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _disk_usage(path: str) -> dict[str, int | float] | None:
    try:
        usage = shutil.disk_usage(path)
        total = usage.total or 1
        return {
            "path": path,
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": round(usage.used / total * 100.0, 1),
        }
    except OSError:
        return None


def _process_count() -> int | None:
    try:
        return sum(1 for name in os.listdir("/proc") if name.isdigit())
    except OSError:
        return None


def collect_system_stats() -> dict:
    mem = _read_meminfo()
    mem_total = mem.get("MemTotal", 0)
    mem_available = mem.get("MemAvailable", mem.get("MemFree", 0))
    mem_used = max(0, mem_total - mem_available) if mem_total else 0
    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free) if swap_total else 0

    disks = []
    for path in ("/", "/var/www/html", "/tmp"):
        d = _disk_usage(path)
        if not d:
            continue
        if any(x["total"] == d["total"] and x["free"] == d["free"] for x in disks):
            continue
        disks.append(d)

    return {
        "cpu_percent": _cpu_percent(),
        "loadavg": _loadavg(),
        "memory": {
            "total_kb": mem_total,
            "used_kb": mem_used,
            "available_kb": mem_available,
            "percent": round(mem_used / mem_total * 100.0, 1) if mem_total else 0.0,
        },
        "swap": {
            "total_kb": swap_total,
            "used_kb": swap_used,
            "percent": round(swap_used / swap_total * 100.0, 1) if swap_total else 0.0,
        },
        "disks": disks,
        "uptime_seconds": _uptime_seconds(),
        "process_count": _process_count(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _mysql_env(password: str) -> dict[str, str]:
    env = dict(os.environ)
    if password:
        env["MYSQL_PWD"] = password
    return env


def dump_database(dest: Path) -> None:
    db = db_settings()
    password = db["root_pass"] or db["pass"]
    user = "root" if db["root_pass"] else db["user"]
    cmd = [
        "mysqldump",
        f"--user={user}",
        "--single-transaction",
        "--routines",
        "--triggers",
        "--databases",
        db["name"],
    ]
    with dest.open("wb") as out:
        proc = subprocess.run(
            cmd,
            stdout=out,
            stderr=subprocess.PIPE,
            env=_mysql_env(password),
            timeout=BACKUP_CREATE_TIMEOUT,
            check=False,
        )
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(err or f"mysqldump exit {proc.returncode}")


def import_database(sql_path: Path) -> None:
    db = db_settings()
    password = db["root_pass"] or db["pass"]
    user = "root" if db["root_pass"] else db["user"]
    with sql_path.open("rb") as inp:
        proc = subprocess.run(
            ["mysql", f"--user={user}"],
            stdin=inp,
            stderr=subprocess.PIPE,
            env=_mysql_env(password),
            timeout=BACKUP_RESTORE_TIMEOUT,
            check=False,
        )
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(err or f"mysql exit {proc.returncode}")


def create_backup_archive() -> Path:
    BACKUP_WORKDIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    work = Path(tempfile.mkdtemp(prefix="bk-", dir=str(BACKUP_WORKDIR)))
    archive = BACKUP_WORKDIR / f"wp-backup-{stamp}.tar.gz"
    try:
        sql_path = work / "database.sql"
        dump_database(sql_path)
        meta = work / "meta.txt"
        meta.write_text(
            f"created_utc={stamp}\n"
            f"db_name={db_settings()['name']}\n"
            f"wp_root={WP_ROOT}\n",
            encoding="utf-8",
        )
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(sql_path, arcname="database.sql")
            tar.add(meta, arcname="meta.txt")
            if WP_ROOT.is_dir():
                tar.add(str(WP_ROOT), arcname="html")
        return archive
    except Exception:
        if archive.exists():
            archive.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Reject path traversal and absolute paths inside the archive."""
    members: list[tarfile.TarInfo] = []
    for member in tar.getmembers():
        name = member.name.replace("\\", "/")
        if name.startswith("/") or name.startswith("..") or "/../" in f"/{name}/":
            raise RuntimeError(f"مسیر نامعتبر در آرشیو: {member.name}")
        members.append(member)
    return members


def restore_from_archive(upload_path: Path) -> None:
    staging = Path(tempfile.mkdtemp(prefix="rs-", dir=str(BACKUP_WORKDIR)))
    try:
        with tarfile.open(upload_path, "r:*") as tar:
            tar.extractall(staging, members=_safe_members(tar))

        sql_path = staging / "database.sql"
        html_src = staging / "html"
        if not sql_path.is_file():
            raise RuntimeError("فایل database.sql در بکاپ پیدا نشد.")
        if not html_src.is_dir():
            raise RuntimeError("پوشه html در بکاپ پیدا نشد.")

        import_database(sql_path)

        WP_ROOT.mkdir(parents=True, exist_ok=True)
        for child in list(WP_ROOT.iterdir()):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)

        for child in html_src.iterdir():
            dest = WP_ROOT / child.name
            if child.is_dir():
                shutil.copytree(child, dest, symlinks=True)
            else:
                shutil.copy2(child, dest)

        _wp_fix_permissions()
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _version_sort_key(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in version.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def wordpress_is_present() -> bool:
    return (WP_ROOT / "wp-load.php").is_file() or (WP_ROOT / "wp-includes").is_dir()


def current_wordpress_version() -> str | None:
    if not wordpress_is_present():
        return None
    try:
        proc = subprocess.run(
            ["wp", "core", "version", f"--path={WP_ROOT}", "--allow-root"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode == 0:
            ver = (proc.stdout or "").strip()
            if ver:
                return ver
    except (OSError, subprocess.TimeoutExpired):
        pass
    version_php = WP_ROOT / "wp-includes" / "version.php"
    if version_php.is_file():
        try:
            text = version_php.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"\$wp_version\s*=\s*'([^']+)'", text)
            if match:
                return match.group(1)
        except OSError:
            pass
    return None


def fetch_wordpress_versions() -> list[dict[str, str]]:
    """List official WordPress releases from wordpress.org stable-check API."""
    req = urllib.request.Request(
        WP_VERSIONS_API,
        headers={"User-Agent": "wp-aio-panel/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError("پاسخ نامعتبر از API نسخه‌های وردپرس")
    items: list[dict[str, str]] = []
    for version, status in data.items():
        if not isinstance(version, str) or not WP_VERSION_RE.match(version):
            continue
        st = str(status) if status is not None else "outdated"
        items.append({"version": version, "status": st})
    items.sort(key=lambda x: _version_sort_key(x["version"]), reverse=True)
    return items


def _wp_fix_permissions() -> None:
    """Make /var/www/html owned/writable by www-data (PHP-FPM + FileBrowser)."""
    WP_ROOT.mkdir(parents=True, exist_ok=True)
    # PHP move_uploaded_file source dir (must exist before FPM handles uploads)
    php_tmp = Path("/tmp/php-uploads")
    php_tmp.mkdir(parents=True, exist_ok=True)
    subprocess.run(["chmod", "1777", "/tmp", str(php_tmp)], check=False, capture_output=True, timeout=30)
    subprocess.run(
        ["chown", "www-data:www-data", str(php_tmp)],
        check=False,
        capture_output=True,
        timeout=30,
    )

    content = WP_ROOT / "wp-content"
    if (WP_ROOT / "wp-load.php").is_file() or (WP_ROOT / "wp-includes").is_dir():
        for sub in ("uploads", "plugins", "themes", "upgrade", "cache", "mu-plugins"):
            (content / sub).mkdir(parents=True, exist_ok=True)
        (content / "upgrade" / "temp").mkdir(parents=True, exist_ok=True)
        y_m = datetime.now(timezone.utc).strftime("%Y/%m")
        (content / "uploads" / y_m).mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["chown", "-R", "www-data:www-data", "/var/www"],
        check=False,
        capture_output=True,
        timeout=180,
    )
    subprocess.run(
        ["find", str(WP_ROOT), "-type", "d", "-exec", "chmod", "755", "{}", "+"],
        check=False,
        capture_output=True,
        timeout=180,
    )
    subprocess.run(
        ["find", str(WP_ROOT), "-type", "f", "-exec", "chmod", "644", "{}", "+"],
        check=False,
        capture_output=True,
        timeout=180,
    )
    if content.is_dir():
        subprocess.run(
            ["find", str(content), "-type", "d", "-exec", "chmod", "2775", "{}", "+"],
            check=False,
            capture_output=True,
            timeout=180,
        )
        subprocess.run(
            ["find", str(content), "-type", "f", "-exec", "chmod", "664", "{}", "+"],
            check=False,
            capture_output=True,
            timeout=180,
        )
        for writable in ("uploads", "plugins", "themes", "upgrade", "cache"):
            target = content / writable
            if target.is_dir():
                subprocess.run(
                    ["chmod", "-R", "u+rwX,g+rwX", str(target)],
                    check=False,
                    capture_output=True,
                    timeout=180,
                )
    cfg = WP_ROOT / "wp-config.php"
    if cfg.is_file():
        subprocess.run(
            ["chown", "www-data:www-data", str(cfg)],
            check=False,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            ["chmod", "640", str(cfg)],
            check=False,
            capture_output=True,
            timeout=30,
        )


def _wp_chown() -> None:
    """Backward-compatible alias used by install/restore paths."""
    _wp_fix_permissions()


def ensure_wordpress_htaccess_file() -> None:
    WP_ROOT.mkdir(parents=True, exist_ok=True)
    dest = WP_ROOT / ".htaccess"
    if WP_HTACCESS_SRC.is_file():
        shutil.copy2(WP_HTACCESS_SRC, dest)
    elif not dest.is_file():
        dest.write_text(
            "# BEGIN WordPress\n"
            "<IfModule mod_rewrite.c>\n"
            "RewriteEngine On\n"
            "RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]\n"
            "RewriteBase /\n"
            "RewriteRule ^index\\.php$ - [L]\n"
            "RewriteCond %{REQUEST_FILENAME} !-f\n"
            "RewriteCond %{REQUEST_FILENAME} !-d\n"
            "RewriteRule . /index.php [L]\n"
            "</IfModule>\n"
            "# END WordPress\n",
            encoding="utf-8",
        )


def _inject_wp_config_block(text: str, marker: str, block: str) -> str:
    """Insert a marked block before wp-settings.php when missing."""
    if marker in text:
        return text
    needle = "require_once ABSPATH . 'wp-settings.php';"
    if needle in text:
        return text.replace(needle, block + needle, 1)
    return text + block


def ensure_wordpress_config() -> None:
    """Create wp-config.php from env DB settings when missing; patch SSL/proxy."""
    cfg = WP_ROOT / "wp-config.php"
    if not cfg.is_file():
        db = db_settings()
        if not (WP_ROOT / "wp-config-sample.php").is_file():
            raise RuntimeError("wp-config-sample.php بعد از دانلود پیدا نشد.")
        cmd = [
            "wp",
            "config",
            "create",
            f"--dbname={db['name']}",
            f"--dbuser={db['user']}",
            f"--dbpass={db['pass']}",
            "--dbhost=localhost",
            f"--path={WP_ROOT}",
            "--allow-root",
            "--skip-check",
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0 or not cfg.is_file():
            err = ((proc.stderr or "") + (proc.stdout or "")).strip()
            raise RuntimeError(err or "ساخت wp-config.php ناموفق بود.")

    # Performance / cron offload + reverse-proxy HTTPS — same intent as init.sh
    text = cfg.read_text(encoding="utf-8", errors="replace")
    original = text
    text = _inject_wp_config_block(
        text,
        "WP_PERF_TUNING",
        (
            "\n/* WP_PERF_TUNING */\n"
            "define('WP_MEMORY_LIMIT', '128M');\n"
            "define('WP_MAX_MEMORY_LIMIT', '256M');\n"
            "define('WP_POST_REVISIONS', 5);\n"
            "define('EMPTY_TRASH_DAYS', 7);\n"
            "define('AUTOSAVE_INTERVAL', 120);\n"
            "define('WP_CRON_LOCK_TIMEOUT', 60);\n"
            "define('DISABLE_WP_CRON', true);\n"
            "define('CONCATENATE_SCRIPTS', true);\n"
            "define('DISALLOW_FILE_EDIT', true);\n"
            "/* END WP_PERF_TUNING */\n\n"
        ),
    )
    text = _inject_wp_config_block(
        text,
        "WP_REVERSE_PROXY_SSL",
        (
            "\n/* WP_REVERSE_PROXY_SSL */\n"
            "if (\n"
            "    (!empty($_SERVER['HTTP_X_FORWARDED_PROTO']) && "
            "strtolower(trim(explode(',', (string) $_SERVER['HTTP_X_FORWARDED_PROTO'])[0])) === 'https')\n"
            "    || (!empty($_SERVER['HTTP_X_FORWARDED_SSL']) && "
            "strtolower((string) $_SERVER['HTTP_X_FORWARDED_SSL']) === 'on')\n"
            ") {\n"
            "    $_SERVER['HTTPS'] = 'on';\n"
            "}\n"
            "/* END WP_REVERSE_PROXY_SSL */\n\n"
        ),
    )
    text = _inject_wp_config_block(
        text,
        "FS_METHOD",
        (
            "\n/* WP_FS_DIRECT */\n"
            "define('FS_METHOD', 'direct');\n"
            "define('FS_CHMOD_DIR', 0775);\n"
            "define('FS_CHMOD_FILE', 0664);\n"
            "/* END WP_FS_DIRECT */\n\n"
        ),
    )
    # Older installs may already have FS_METHOD without chmod constants.
    text = _inject_wp_config_block(
        text,
        "FS_CHMOD_DIR",
        (
            "\n/* WP_FS_CHMOD */\n"
            "define('FS_CHMOD_DIR', 0775);\n"
            "define('FS_CHMOD_FILE', 0664);\n"
            "/* END WP_FS_CHMOD */\n\n"
        ),
    )
    if text != original:
        cfg.write_text(text, encoding="utf-8")


def install_wordpress(version: str, preserve_content: bool) -> str:
    """Download and extract WordPress core via WP-CLI into WP_ROOT."""
    version = (version or "").strip()
    if not WP_VERSION_RE.match(version):
        raise ValueError("نسخهٔ انتخاب‌شده معتبر نیست.")

    WP_ROOT.mkdir(parents=True, exist_ok=True)
    present = wordpress_is_present()
    cmd = [
        "wp",
        "core",
        "download",
        f"--version={version}",
        f"--path={WP_ROOT}",
        "--allow-root",
    ]
    if present:
        cmd.append("--force")
        if preserve_content:
            cmd.append("--skip-content")

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=WP_INSTALL_TIMEOUT,
        check=False,
    )
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        raise RuntimeError(out or f"wp core download exit {proc.returncode}")

    # Remove placeholder index left by the image when no WP was installed
    placeholder = WP_ROOT / "index.html"
    if placeholder.is_file() and (WP_ROOT / "wp-load.php").is_file():
        placeholder.unlink(missing_ok=True)

    ensure_wordpress_config()
    ensure_wordpress_htaccess_file()
    _wp_chown()
    return out or f"WordPress {version} نصب شد."


@app.context_processor
def inject_globals():
    return {
        "panel_user": panel_user(),
        "logged_in": bool(session.get("panel_ok")),
    }


@app.route("/panel")
@app.route("/panel/")
def root():
    if session.get("panel_ok"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/panel/login", methods=["GET", "POST"])
def login():
    if session.get("panel_ok"):
        return redirect(url_for("dashboard"))

    error = None
    ip = client_ip()
    remaining = login_lockout_remaining(ip)
    if remaining > 0:
        error = login_lockout_message(remaining)
    elif request.method == "POST":
        user = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        expected_pass = panel_pass()
        ok_user = secure_str_eq(user, panel_user())
        ok_pass = secure_str_eq(password, expected_pass) if expected_pass else False
        if ok_user and ok_pass:
            clear_login_failures(ip)
            session["panel_ok"] = True
            session.permanent = True
            nxt = request.args.get("next") or request.form.get("next") or url_for("dashboard")
            # Only allow relative next paths
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("dashboard")
            resp = redirect(nxt)
            return set_gate_cookie(resp)
        lock_remaining = record_login_failure(ip)
        if lock_remaining > 0:
            error = login_lockout_message(lock_remaining)
        else:
            error = "نام کاربری یا رمز عبور نادرست است."

    next_url = request.args.get("next", "")
    return render_template("login.html", error=error, next_url=next_url)


@app.route("/panel/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    resp = redirect(url_for("login"))
    return clear_gate_cookie(resp)


@app.route("/panel/dashboard")
@login_required
def dashboard():
    host = request.host_url.rstrip("/")
    settings = load_panel_settings()
    return set_gate_cookie(
        app.make_response(
            render_template(
                "dashboard.html",
                filebrowser_url=f"{host}{settings['filebrowser_path']}",
                phpmyadmin_url=f"{host}{settings['phpmyadmin_path']}",
                files=allowed_config_files(),
                wordpress_present=wordpress_is_present(),
            )
        )
    )


@app.route("/panel/settings", methods=["GET", "POST"])
@login_required
def settings():
    message = None
    err = None
    current = load_panel_settings()

    if request.method == "POST":
        fb = normalize_public_path(request.form.get("filebrowser_path") or "")
        pma = normalize_public_path(request.form.get("phpmyadmin_path") or "")
        # PHP version is managed on /panel/php — preserve the stored value here
        php_ver = normalize_php_version(
            current.get("php_version") or ""
        ) or DEFAULT_PHP_VERSION
        err = validate_public_path(fb, label="FileBrowser") or validate_public_path(
            pma, label="phpMyAdmin"
        )
        if not err and fb.lower() == pma.lower():
            err = "مسیر FileBrowser و phpMyAdmin نباید یکسان باشند."
        if err:
            current = {
                "filebrowser_path": fb or current["filebrowser_path"],
                "phpmyadmin_path": pma or current["phpmyadmin_path"],
                "php_version": php_ver,
            }
        else:
            try:
                current = ensure_tool_paths_applied(
                    {
                        "filebrowser_path": fb,
                        "phpmyadmin_path": pma,
                        "php_version": php_ver,
                    },
                    restart_filebrowser=True,
                    reload_apache=True,
                )
                current = load_panel_settings()
                message = (
                    "تنظیمات ذخیره شد. "
                    f"FileBrowser: {fb} — phpMyAdmin: {pma}"
                )
            except Exception as exc:  # noqa: BLE001
                err = f"اعمال تنظیمات ناموفق بود: {exc}"

    return set_gate_cookie(
        app.make_response(
            render_template(
                "settings.html",
                settings=current,
                defaults=default_panel_settings(),
                runtime_php=runtime_php_version(),
                message=message,
                error=err,
            )
        )
    )


@app.route("/panel/configs", methods=["GET", "POST"])
@login_required
def configs():
    files = allowed_config_files()
    selected = request.values.get("path") or (files[0] if files else "")
    content = ""
    message = None
    err = None

    safe = resolve_allowed(selected) if selected else None
    if request.method == "POST":
        action = request.form.get("action") or "save"
        path = request.form.get("path") or ""
        safe = resolve_allowed(path)
        if not safe:
            err = "مسیر مجاز نیست."
        elif action == "save":
            new_content = request.form.get("content")
            if new_content is None:
                err = "محتوا خالی ارسال شد."
            else:
                try:
                    Path(safe).write_text(new_content, encoding="utf-8")
                    message = f"ذخیره شد: {safe}"
                    content = new_content
                    selected = safe
                except OSError as exc:
                    err = f"خطا در ذخیره: {exc}"
        elif action == "restart_php":
            try:
                subprocess.run(
                    ["supervisorctl", "restart", "php-fpm"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                message = "PHP-FPM ری‌استارت شد."
            except Exception as exc:  # noqa: BLE001
                err = f"ری‌استارت PHP-FPM ناموفق: {exc}"
        elif action == "reload_apache":
            try:
                subprocess.run(
                    ["apache2ctl", "graceful"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                message = "Apache با graceful reload به‌روز شد."
            except Exception as exc:  # noqa: BLE001
                err = f"Reload آپاچی ناموفق: {exc}"

    if safe and not content:
        try:
            content = Path(safe).read_text(encoding="utf-8", errors="replace")
            selected = safe
        except OSError as exc:
            err = err or f"خواندن فایل ممکن نشد: {exc}"

    return set_gate_cookie(
        app.make_response(
            render_template(
                "configs.html",
                files=files,
                selected=selected,
                content=content,
                message=message,
                error=err,
            )
        )
    )


@app.route("/panel/monitor")
@login_required
def monitor():
    return set_gate_cookie(
        app.make_response(
            render_template("monitor.html", stats=collect_system_stats())
        )
    )


@app.route("/panel/api/stats")
@login_required
def api_stats():
    return jsonify(collect_system_stats())


@app.route("/panel/wordpress", methods=["GET", "POST"])
@login_required
def wordpress():
    message = None
    err = None
    versions: list[dict[str, str]] = []
    versions_error = None
    current = current_wordpress_version()
    present = wordpress_is_present()

    try:
        versions = fetch_wordpress_versions()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
        versions_error = f"دریافت فهرست نسخه‌ها ناموفق بود: {exc}"
    except Exception as exc:  # noqa: BLE001
        versions_error = f"دریافت فهرست نسخه‌ها ناموفق بود: {exc}"

    if request.method == "POST":
        action = request.form.get("action") or ""
        if action == "install":
            version = (request.form.get("version") or "").strip()
            preserve = request.form.get("preserve_content") == "1"
            allowed = {v["version"] for v in versions} if versions else set()
            if versions and version not in allowed:
                err = "نسخه در فهرست رسمی وردپرس نیست."
            else:
                try:
                    was_present = present
                    detail = install_wordpress(version, preserve_content=preserve)
                    current = current_wordpress_version()
                    present = wordpress_is_present()
                    if was_present:
                        message = f"وردپرس با موفقیت به نسخهٔ {version} آپدیت شد."
                    else:
                        message = f"وردپرس {version} با موفقیت نصب شد."
                    if detail:
                        message += f" ({detail[:200]})"
                except Exception as exc:  # noqa: BLE001
                    err = (
                        f"آپدیت ناموفق: {exc}"
                        if present
                        else f"نصب ناموفق: {exc}"
                    )
        else:
            err = "عملیات نامعتبر."

    return set_gate_cookie(
        app.make_response(
            render_template(
                "wordpress.html",
                versions=versions,
                versions_error=versions_error,
                current_version=current,
                wordpress_present=present,
                message=message,
                error=err,
            )
        )
    )


def _load_php_versions_list(
    *, force_refresh: bool = False
) -> tuple[list[dict[str, str | bool]], str | None]:
    """Fetch apt PHP versions; returns (items, error_message)."""
    try:
        return fetch_php_versions(force_refresh=force_refresh), None
    except (OSError, subprocess.SubprocessError, RuntimeError, TimeoutError) as exc:
        return [], f"دریافت فهرست نسخه‌های PHP از apt ناموفق بود: {exc}"
    except Exception as exc:  # noqa: BLE001
        return [], f"دریافت فهرست نسخه‌های PHP از apt ناموفق بود: {exc}"


@app.route("/panel/php", methods=["GET", "POST"])
@login_required
def php():
    message = None
    err = None
    versions: list[dict[str, str | bool]] = []
    versions_error = None
    active = normalize_php_version(
        load_panel_settings().get("php_version") or ""
    ) or DEFAULT_PHP_VERSION
    runtime = runtime_php_version()

    # apt list (apt-get update) is expensive — only on explicit refresh / after install.
    # GET may reuse in-memory cache without hitting apt.
    if request.method == "GET":
        cached_items = _php_versions_cache.get("items") or []
        cached_at = float(_php_versions_cache.get("at") or 0.0)
        if (
            isinstance(cached_items, list)
            and cached_items
            and (time.time() - cached_at) < PHP_VERSIONS_CACHE_TTL
        ):
            versions, versions_error = _load_php_versions_list(force_refresh=False)
    elif request.method == "POST":
        action = (request.form.get("action") or "").strip()
        version = normalize_php_version(request.form.get("version") or "")
        if action == "refresh":
            versions, versions_error = _load_php_versions_list(force_refresh=True)
            if not versions_error:
                message = "فهرست نسخه‌ها و آپدیت‌های apt به‌روز شد."
        elif action == "install":
            versions, versions_error = _load_php_versions_list()
            allowed = {str(v["version"]) for v in versions} if versions else set()
            if not version:
                err = "نسخه PHP را انتخاب کنید."
            elif versions_error:
                err = versions_error
            elif versions and version not in allowed:
                err = "نسخه در فهرست apt این کانتینر نیست یا بستهٔ کامل ندارد."
            else:
                try:
                    detail = install_or_update_php(version)
                    versions, versions_error = _load_php_versions_list(
                        force_refresh=True
                    )
                    active = normalize_php_version(
                        load_panel_settings().get("php_version") or ""
                    ) or DEFAULT_PHP_VERSION
                    runtime = runtime_php_version()
                    message = f"PHP {version} با موفقیت نصب/آپدیت شد."
                    if detail:
                        message += f" ({detail[:200]})"
                except Exception as exc:  # noqa: BLE001
                    err = f"نصب/آپدیت PHP ناموفق: {exc}"
        elif action == "activate":
            if not version:
                err = "نسخه PHP را انتخاب کنید."
            else:
                activate_err = validate_php_version(version)
                if activate_err:
                    err = activate_err
                else:
                    try:
                        applied = ensure_php_version_applied(
                            version, restart_fpm=True
                        )
                        if applied != version:
                            err = (
                                f"فعال‌سازی PHP {version} انجام نشد "
                                f"(نتیجه: {applied})."
                            )
                        else:
                            active = applied
                            runtime = runtime_php_version()
                            cached_items = _php_versions_cache.get("items") or []
                            cached_at = float(
                                _php_versions_cache.get("at") or 0.0
                            )
                            if (
                                isinstance(cached_items, list)
                                and cached_items
                                and (time.time() - cached_at)
                                < PHP_VERSIONS_CACHE_TTL
                            ):
                                versions = _annotate_php_version_items(
                                    [
                                        dict(x)
                                        for x in cached_items
                                        if isinstance(x, dict)
                                    ],
                                    active,
                                )
                            message = (
                                f"PHP {applied} فعال شد و PHP-FPM ری‌استارت شد."
                            )
                    except Exception as exc:  # noqa: BLE001
                        err = f"فعال‌سازی PHP ناموفق: {exc}"
        else:
            err = "عملیات نامعتبر."

    installed = [
        str(v["version"])
        for v in versions
        if v.get("has_binary") or v.get("status") in ("active", "installed", "update")
    ]
    if not installed:
        installed = installed_php_versions()
    else:
        installed = sorted(set(installed), key=_version_sort_key, reverse=True)

    return set_gate_cookie(
        app.make_response(
            render_template(
                "php.html",
                versions=versions,
                versions_error=versions_error,
                active_version=active,
                runtime_php=runtime,
                installed_versions=installed,
                message=message,
                error=err,
            )
        )
    )


def run_shell_command(command: str) -> dict:
    """Same container shell mechanism; returns structured result for console UI."""
    command = (command or "").strip()
    if not command:
        return {"ok": False, "command": "", "output": "دستوری وارد نشده.", "exit_code": None}
    if len(command) > 4000:
        return {
            "ok": False,
            "command": command[:80] + "…",
            "output": "دستور خیلی طولانی است.",
            "exit_code": None,
        }
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT,
            cwd="/",
            env={**os.environ, "TERM": "xterm"},
        )
        parts = []
        if proc.stdout:
            parts.append(proc.stdout)
        if proc.stderr:
            parts.append(proc.stderr)
        output = "".join(parts)
        if len(output) > SHELL_MAX_OUTPUT:
            output = output[:SHELL_MAX_OUTPUT] + "\n… [خروجی کوتاه شد]"
        return {
            "ok": True,
            "command": command,
            "output": output,
            "exit_code": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "command": command,
            "output": f"زمان اجرا از {SHELL_TIMEOUT} ثانیه بیشتر شد؛ فرایند قطع شد.",
            "exit_code": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "command": command,
            "output": f"خطا: {exc}",
            "exit_code": None,
        }


@app.route("/panel/shell", methods=["GET", "POST"])
@login_required
def shell():
    # Interactive console is client-driven; POST kept for no-JS fallback.
    if request.method == "POST":
        result = run_shell_command(request.form.get("command") or "")
        return set_gate_cookie(
            app.make_response(
                render_template(
                    "shell.html",
                    command=result["command"],
                    output=result["output"],
                    exit_code=result["exit_code"],
                    shell_timeout=SHELL_TIMEOUT,
                )
            )
        )
    return set_gate_cookie(
        app.make_response(
            render_template(
                "shell.html",
                command="",
                output="",
                exit_code=None,
                shell_timeout=SHELL_TIMEOUT,
            )
        )
    )


@app.route("/panel/api/shell", methods=["POST"])
@login_required
def api_shell():
    data = request.get_json(silent=True) or {}
    command = data.get("command") if isinstance(data, dict) else None
    if command is None:
        command = request.form.get("command") or ""
    return jsonify(run_shell_command(command))


_CHAT_TURNS: dict[str, threading.Event] = {}
_CHAT_TURNS_LOCK = threading.Lock()
_TURN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


def _register_chat_turn(turn_id: str) -> threading.Event:
    with _CHAT_TURNS_LOCK:
        event = _CHAT_TURNS.get(turn_id)
        if event is None:
            event = threading.Event()
            if len(_CHAT_TURNS) > 40:
                for key in list(_CHAT_TURNS)[:20]:
                    _CHAT_TURNS.pop(key, None)
            _CHAT_TURNS[turn_id] = event
        return event


def _cancel_chat_turn(turn_id: str) -> None:
    with _CHAT_TURNS_LOCK:
        event = _CHAT_TURNS.get(turn_id)
        if event is None:
            event = threading.Event()
            if len(_CHAT_TURNS) > 40:
                for key in list(_CHAT_TURNS)[:20]:
                    _CHAT_TURNS.pop(key, None)
            _CHAT_TURNS[turn_id] = event
        event.set()


def _finish_chat_turn(turn_id: str) -> None:
    with _CHAT_TURNS_LOCK:
        _CHAT_TURNS.pop(turn_id, None)


CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
MAX_STORED_CHATS = 40
DEFAULT_CHAT_TITLE = "گفتگوی جدید"
_chat_store_lock = threading.Lock()
_chat_running: set[str] = set()


class _ChatStoreError(Exception):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def _fresh_chat_id(store: dict) -> str:
    used = {chat["id"] for chat in store.get("chats") or []}
    for _ in range(8):
        chat_id = secrets.token_urlsafe(12)
        if CHAT_ID_RE.fullmatch(chat_id) and chat_id not in used:
            return chat_id
    chat_id = secrets.token_hex(16)
    while chat_id in used:
        chat_id = secrets.token_hex(16)
    return chat_id


def _title_from_text(text: str) -> str:
    line = " ".join(text.split())
    if not line:
        return DEFAULT_CHAT_TITLE
    if len(line) > 48:
        return line[:48].rstrip() + "…"
    return line


def _is_default_title(title: str) -> bool:
    return " ".join((title or "").split()) in ("", DEFAULT_CHAT_TITLE)


def _as_timestamp(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _sanitize_chat(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    chat_id = raw.get("id")
    if not isinstance(chat_id, str) or not CHAT_ID_RE.fullmatch(chat_id):
        return None
    title = raw.get("title") if isinstance(raw.get("title"), str) else DEFAULT_CHAT_TITLE
    title = " ".join(title.split())[:80] or DEFAULT_CHAT_TITLE
    raw_messages = raw.get("messages") if isinstance(raw.get("messages"), list) else []
    created = _as_timestamp(raw.get("created_at"))
    updated = _as_timestamp(raw.get("updated_at"))
    if updated <= 0:
        updated = created or time.time()
    if created <= 0:
        created = updated
    return {
        "id": chat_id,
        "title": title,
        "created_at": created,
        "updated_at": updated,
        "messages": _normalize_history(raw_messages),
    }


def _sanitize_store(data: dict) -> dict:
    chats: list[dict] = []
    index: dict[str, int] = {}
    raw_chats = data.get("chats")
    if isinstance(raw_chats, list):
        for raw in raw_chats:
            chat = _sanitize_chat(raw)
            if chat is None:
                continue
            pos = index.get(chat["id"])
            if pos is None:
                index[chat["id"]] = len(chats)
                chats.append(chat)
            elif chat["updated_at"] >= chats[pos]["updated_at"]:
                chats[pos] = chat
    chats.sort(key=lambda item: item["updated_at"], reverse=True)
    del chats[MAX_STORED_CHATS:]
    active = data.get("active_id")
    known = {item["id"] for item in chats}
    if not isinstance(active, str) or active not in known:
        active = chats[0]["id"] if chats else ""
    return {"active_id": active, "chats": chats}


def _load_chat_store() -> dict:
    try:
        if CHAT_STORE_PATH.is_file():
            data = json.loads(CHAT_STORE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return _sanitize_store(data)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return {"active_id": "", "chats": []}


def _save_chat_store(store: dict) -> None:
    CHAT_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "active_id": store.get("active_id") or "",
        "chats": store.get("chats") or [],
    }
    tmp = CHAT_STORE_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(CHAT_STORE_PATH)


def _chat_summary(chat: dict) -> dict:
    messages = chat.get("messages") if isinstance(chat.get("messages"), list) else []
    return {
        "id": chat["id"],
        "title": chat.get("title") or DEFAULT_CHAT_TITLE,
        "updated_at": chat.get("updated_at") or 0,
        "message_count": len(messages),
        "running": chat["id"] in _chat_running,
    }


def _chat_detail(chat: dict) -> dict:
    detail = _chat_summary(chat)
    detail["messages"] = list(chat.get("messages") or [])
    return detail


def _find_chat(store: dict, chat_id: str) -> dict | None:
    for chat in store["chats"]:
        if chat["id"] == chat_id:
            return chat
    return None


def _sort_chats(store: dict) -> None:
    store["chats"].sort(key=lambda item: item.get("updated_at") or 0, reverse=True)


def _blank_chat(store: dict) -> dict:
    now = time.time()
    return {
        "id": _fresh_chat_id(store),
        "title": DEFAULT_CHAT_TITLE,
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }


def _list_chats() -> dict:
    with _chat_store_lock:
        store = _load_chat_store()
        if not store["chats"]:
            chat = _blank_chat(store)
            store["chats"] = [chat]
            store["active_id"] = chat["id"]
            _save_chat_store(store)
        return store


def _create_chat() -> dict:
    with _chat_store_lock:
        store = _load_chat_store()
        if len(store["chats"]) >= MAX_STORED_CHATS:
            raise _ChatStoreError(
                "تعداد گفتگوها به سقف ۴۰ رسیده است. یکی را حذف کنید.",
                400,
            )
        chat = _blank_chat(store)
        store["chats"].insert(0, chat)
        store["active_id"] = chat["id"]
        _sort_chats(store)
        _save_chat_store(store)
        return chat


def _open_chat(chat_id: str) -> dict:
    with _chat_store_lock:
        store = _load_chat_store()
        chat = _find_chat(store, chat_id)
        if chat is None:
            raise _ChatStoreError("این گفتگو پیدا نشد.", 404)
        if store["active_id"] != chat_id:
            store["active_id"] = chat_id
            _save_chat_store(store)
        return chat


def _delete_chat(chat_id: str) -> dict:
    with _chat_store_lock:
        if chat_id in _chat_running:
            raise _ChatStoreError("این گفتگو هنوز در حال پاسخ است.", 409)
        store = _load_chat_store()
        kept = [chat for chat in store["chats"] if chat["id"] != chat_id]
        if len(kept) == len(store["chats"]):
            raise _ChatStoreError("این گفتگو پیدا نشد.", 404)
        store["chats"] = kept
        if not store["chats"]:
            chat = _blank_chat(store)
            store["chats"] = [chat]
            store["active_id"] = chat["id"]
        elif store["active_id"] == chat_id:
            store["active_id"] = store["chats"][0]["id"]
        _sort_chats(store)
        _save_chat_store(store)
        return store


def _begin_chat_turn(chat_id: str, text: str) -> list[dict[str, str]]:
    """Append the user message and return the older turns of this chat only."""
    with _chat_store_lock:
        if chat_id in _chat_running:
            raise _ChatStoreError("این گفتگو هنوز در حال پاسخ است.", 409)
        store = _load_chat_store()
        chat = _find_chat(store, chat_id)
        if chat is None:
            raise _ChatStoreError("این گفتگو پیدا نشد.", 404)
        stored = _normalize_history(
            list(chat["messages"]) + [{"role": "user", "content": text}]
        )
        if not stored or stored[-1].get("role") != "user":
            raise _ChatStoreError("پیام خالی است.", 400)
        history = [dict(item) for item in stored[:-1]]
        chat["messages"] = stored
        if _is_default_title(str(chat.get("title") or "")):
            chat["title"] = _title_from_text(text)
        chat["updated_at"] = time.time()
        store["active_id"] = chat_id
        _sort_chats(store)
        _save_chat_store(store)
        _chat_running.add(chat_id)
        return history


def _commit_chat_turn(chat_id: str, reply: str | None) -> dict | None:
    with _chat_store_lock:
        _chat_running.discard(chat_id)
        store = _load_chat_store()
        chat = _find_chat(store, chat_id)
        if chat is None:
            return None
        if reply:
            chat["messages"] = _normalize_history(
                list(chat["messages"]) + [{"role": "assistant", "content": reply}]
            )
            chat["updated_at"] = time.time()
            _sort_chats(store)
            _save_chat_store(store)
        return _chat_summary(chat)


@app.route("/panel/chat")
@login_required
def chat():
    return set_gate_cookie(
        app.make_response(
            render_template(
                "chat.html",
                agent_ready=agent_configured(),
            )
        )
    )


@app.route("/panel/api/chat/cancel", methods=["POST"])
@login_required
def api_chat_cancel():
    data = request.get_json(silent=True) or {}
    turn_id = data.get("turn_id") if isinstance(data, dict) else None
    turn_id = turn_id.strip() if isinstance(turn_id, str) else ""
    if not _TURN_ID_RE.fullmatch(turn_id):
        return jsonify({"ok": False, "error": "شناسهٔ نوبت نامعتبر است."}), 400
    _cancel_chat_turn(turn_id)
    return jsonify({"ok": True})


def _chat_failure(message: str, status: int):
    return jsonify({"ok": False, "error": message, "reply": "", "steps": []}), status


@app.route("/panel/api/chats", methods=["GET", "POST"])
@login_required
def api_chats():
    if request.method == "POST":
        try:
            chat = _create_chat()
        except _ChatStoreError as exc:
            return jsonify({"ok": False, "error": str(exc)}), exc.status
        return jsonify({"ok": True, "chat": _chat_detail(chat)})
    store = _list_chats()
    return jsonify(
        {
            "ok": True,
            "active_id": store["active_id"],
            "chats": [_chat_summary(chat) for chat in store["chats"]],
        }
    )


@app.route("/panel/api/chats/<chat_id>", methods=["GET", "DELETE"])
@login_required
def api_chat_item(chat_id: str):
    if not CHAT_ID_RE.fullmatch(chat_id):
        return jsonify({"ok": False, "error": "شناسهٔ گفتگو نامعتبر است."}), 400
    if request.method == "DELETE":
        try:
            store = _delete_chat(chat_id)
        except _ChatStoreError as exc:
            return jsonify({"ok": False, "error": str(exc)}), exc.status
        return jsonify(
            {
                "ok": True,
                "active_id": store["active_id"],
                "chats": [_chat_summary(chat) for chat in store["chats"]],
            }
        )
    try:
        chat = _open_chat(chat_id)
    except _ChatStoreError as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status
    return jsonify({"ok": True, "chat": _chat_detail(chat)})


@app.route("/panel/api/chat", methods=["POST"])
@login_required
def api_chat():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return _chat_failure("درخواست نامعتبر است.", 400)
    message = data.get("message") or ""
    history = data.get("history") if isinstance(data.get("history"), list) else []
    raw_chat = data.get("chat_id")
    chat_id = raw_chat.strip() if isinstance(raw_chat, str) else ""
    raw_turn = data.get("turn_id")
    turn_id = raw_turn.strip() if isinstance(raw_turn, str) else ""
    if turn_id and not _TURN_ID_RE.fullmatch(turn_id):
        return _chat_failure("شناسهٔ نوبت نامعتبر است.", 400)
    user_text = str(message)
    if chat_id:
        if not CHAT_ID_RE.fullmatch(chat_id):
            return _chat_failure("شناسهٔ گفتگو نامعتبر است.", 400)
        user_text = user_text.strip()
        if not user_text:
            return _chat_failure("پیام خالی است.", 400)
        if len(user_text) > 20_000:
            return _chat_failure("پیام خیلی بلند است.", 400)

    cancelled = _register_chat_turn(turn_id) if turn_id else None
    started = False
    result: dict | None = None
    status = 200
    store_error: _ChatStoreError | None = None
    try:
        if chat_id:
            # Memory is this chat's stored transcript, not the browser payload.
            history = _begin_chat_turn(chat_id, user_text)
            started = True
        try:
            result = run_agent_turn(user_text, history, cancelled=cancelled)
        except AgentCancelled:
            result = {
                "ok": False,
                "cancelled": True,
                "error": "کار ایجنت لغو شد.",
                "reply": "",
                "steps": [],
                "configured": agent_configured(),
            }
    except _ChatStoreError as exc:
        store_error = exc
    except Exception as exc:  # noqa: BLE001 — return JSON error to UI
        result = {
            "ok": False,
            "error": f"خطای داخلی ایجنت: {exc}",
            "reply": "",
            "steps": [],
            "configured": agent_configured(),
        }
        status = 500
    finally:
        if turn_id:
            _finish_chat_turn(turn_id)

    if store_error is not None:
        return _chat_failure(str(store_error), store_error.status)

    if started:
        reply = ""
        if isinstance(result, dict) and result.get("ok"):
            raw_reply = result.get("reply")
            if isinstance(raw_reply, str):
                reply = raw_reply.strip()
        try:
            meta = _commit_chat_turn(chat_id, reply or None)
        except Exception:
            with _chat_store_lock:
                _chat_running.discard(chat_id)
            meta = None
        if isinstance(result, dict) and meta:
            result["chat"] = meta

    if not isinstance(result, dict):
        return _chat_failure("خطای داخلی ایجنت.", 500)
    if status == 200 and not (result.get("ok") or result.get("cancelled")):
        status = 400
    return jsonify(result), status


def _render_backup_page(message: str | None = None, err: str | None = None):
    schedule = load_backup_settings()
    risk = assess_backup_storage_risk(schedule)
    return set_gate_cookie(
        app.make_response(
            render_template(
                "backup.html",
                message=message,
                error=err,
                schedule=schedule,
                risk=risk,
                weekdays=WEEKDAY_LABELS,
                full_backups=list_stored_backups("full"),
                db_backups=list_stored_backups("db"),
            )
        )
    )


@app.route("/panel/backup", methods=["GET", "POST"])
@login_required
def backup():
    message = None
    err = None

    if request.method == "POST":
        action = request.form.get("action") or ""
        if action == "save_schedule":
            settings, parse_err = parse_backup_settings_form(request.form)
            if parse_err:
                err = parse_err
            else:
                risk = assess_backup_storage_risk(settings)
                if risk["level"] == "danger" and request.form.get("ack_risk") != "1":
                    err = (
                        "این تنظیمات ممکن است دیسک سرور را پر کند. "
                        "هشدارها را بخوانید و در صورت اطمینان، تأیید خطر را بزنید."
                    )
                    # Keep drafted values visible
                    return set_gate_cookie(
                        app.make_response(
                            render_template(
                                "backup.html",
                                message=None,
                                error=err,
                                schedule=settings,
                                risk=risk,
                                weekdays=WEEKDAY_LABELS,
                                full_backups=list_stored_backups("full"),
                                db_backups=list_stored_backups("db"),
                                require_risk_ack=True,
                            )
                        )
                    )
                try:
                    save_backup_settings(settings)
                    message = "تنظیمات زمان‌بندی بکاپ ذخیره شد."
                    if risk["warnings"]:
                        message += " هشدار فضای دیسک را هم بررسی کنید."
                except Exception as exc:  # noqa: BLE001
                    err = f"ذخیرهٔ تنظیمات ناموفق: {exc}"
        elif action == "run_full_now":
            try:
                path = create_stored_full_backup()
                message = f"بکاپ کامل ذخیره شد: {path.name}"
            except Exception as exc:  # noqa: BLE001
                err = f"بکاپ کامل ناموفق: {exc}"
        elif action == "run_db_now":
            try:
                path = create_stored_db_backup()
                message = f"بکاپ دیتابیس ذخیره شد: {path.name}"
            except Exception as exc:  # noqa: BLE001
                err = f"بکاپ دیتابیس ناموفق: {exc}"
        elif action == "restore_stored_full":
            name = request.form.get("name") or ""
            try:
                restore_stored_full(name)
                message = f"ریستور بکاپ کامل «{name}» انجام شد."
            except Exception as exc:  # noqa: BLE001
                err = f"ریستور کامل ناموفق: {exc}"
        elif action == "restore_stored_db":
            name = request.form.get("name") or ""
            try:
                restore_stored_db(name)
                message = f"ریستور دیتابیس از «{name}» انجام شد."
            except Exception as exc:  # noqa: BLE001
                err = f"ریستور دیتابیس ناموفق: {exc}"
        elif action == "delete_stored":
            kind = request.form.get("kind") or ""
            name = request.form.get("name") or ""
            try:
                delete_stored_backup(kind, name)
                message = f"بکاپ «{name}» حذف شد."
            except Exception as exc:  # noqa: BLE001
                err = f"حذف ناموفق: {exc}"
        elif action == "restore":
            uploaded = request.files.get("backup_file")
            if not uploaded or not uploaded.filename:
                err = "فایل بکاپ انتخاب نشده."
            else:
                name = uploaded.filename.lower()
                if not (name.endswith(".tar.gz") or name.endswith(".tgz")):
                    err = "فقط فایل‌های .tar.gz یا .tgz پذیرفته می‌شوند."
                else:
                    BACKUP_WORKDIR.mkdir(parents=True, exist_ok=True)
                    fd, tmp_name = tempfile.mkstemp(
                        prefix="up-", suffix=".tar.gz", dir=str(BACKUP_WORKDIR)
                    )
                    os.close(fd)
                    tmp = Path(tmp_name)
                    try:
                        uploaded.save(tmp)
                        restore_from_archive(tmp)
                        message = "ریستور بکاپ با موفقیت انجام شد (فایل‌ها + دیتابیس)."
                    except Exception as exc:  # noqa: BLE001
                        err = f"ریستور ناموفق: {exc}"
                    finally:
                        tmp.unlink(missing_ok=True)
        else:
            err = "عملیات نامعتبر."

    return _render_backup_page(message, err)


@app.route("/panel/backup/download")
@login_required
def backup_download():
    archive: Path | None = None
    try:
        archive = create_backup_archive()
        return set_gate_cookie(
            send_file(
                archive,
                as_attachment=True,
                download_name=archive.name,
                mimetype="application/gzip",
            )
        )
    except Exception as exc:  # noqa: BLE001
        return _render_backup_page(None, f"ساخت بکاپ ناموفق: {exc}")
    finally:
        # send_file may still be streaming; schedule cleanup of old archives
        try:
            BACKUP_WORKDIR.mkdir(parents=True, exist_ok=True)
            for old in BACKUP_WORKDIR.glob("wp-backup-*.tar.gz"):
                if archive is not None and old.resolve() == archive.resolve():
                    continue
                try:
                    if old.stat().st_mtime < (
                        datetime.now(timezone.utc).timestamp() - 3600
                    ):
                        old.unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError:
            pass


@app.route("/panel/backup/file/<kind>/<path:name>")
@login_required
def backup_file_download(kind: str, name: str):
    path = resolve_stored_backup(kind, name)
    if path is None:
        return _render_backup_page(None, "فایل بکاپ برای دانلود پیدا نشد.")
    mime = "application/gzip"
    return set_gate_cookie(
        send_file(
            path,
            as_attachment=True,
            download_name=path.name,
            mimetype=mime,
        )
    )


# Avoid open redirects from crafted Host — links use request.host_url only for same host
@app.after_request
def security_headers(resp):
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=9090)
