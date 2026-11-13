#!/usr/bin/env python3
"""Admin panel at /panel — login, config edit, shell, backup, links to tools."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
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
DEFAULT_FILEBROWSER_PATH = "/filebrowser"
DEFAULT_PHPMYADMIN_PATH = "/phpmyadmin"
DEFAULT_PHP_VERSION = os.environ.get("DEFAULT_PHP_VERSION", "8.1").strip() or "8.1"
AVAILABLE_PHP_VERSIONS = tuple(
    v
    for v in re.split(r"[\s,]+", os.environ.get("PHP_VERSIONS", "8.1 8.2 8.3 8.4"))
    if re.fullmatch(r"\d+\.\d+", v)
) or ("8.1", "8.2", "8.3", "8.4")
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
    found = [
        ver
        for ver in AVAILABLE_PHP_VERSIONS
        if Path(f"/usr/sbin/php-fpm{ver}").is_file()
    ]
    return found or [DEFAULT_PHP_VERSION]


def validate_php_version(version: str) -> str | None:
    if not version:
        return "نسخه PHP را انتخاب کنید."
    if version not in AVAILABLE_PHP_VERSIONS:
        return f"نسخه PHP {version} پشتیبانی نمی‌شود."
    if not Path(f"/usr/sbin/php-fpm{version}").is_file():
        return f"PHP {version} روی این ایمیج نصب نیست."
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
    if request.method == "POST":
        user = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        expected_pass = panel_pass()
        ok_user = secure_str_eq(user, panel_user())
        ok_pass = secure_str_eq(password, expected_pass) if expected_pass else False
        if ok_user and ok_pass:
            session["panel_ok"] = True
            session.permanent = True
            nxt = request.args.get("next") or request.form.get("next") or url_for("dashboard")
            # Only allow relative next paths
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("dashboard")
            resp = redirect(nxt)
            return set_gate_cookie(resp)
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
    php_choices = installed_php_versions()

    if request.method == "POST":
        fb = normalize_public_path(request.form.get("filebrowser_path") or "")
        pma = normalize_public_path(request.form.get("phpmyadmin_path") or "")
        php_ver = normalize_php_version(request.form.get("php_version") or "")
        err = validate_public_path(fb, label="FileBrowser") or validate_public_path(
            pma, label="phpMyAdmin"
        )
        if not err and fb.lower() == pma.lower():
            err = "مسیر FileBrowser و phpMyAdmin نباید یکسان باشند."
        if not err:
            err = validate_php_version(php_ver)
        if err:
            current = {
                "filebrowser_path": fb or current["filebrowser_path"],
                "phpmyadmin_path": pma or current["phpmyadmin_path"],
                "php_version": php_ver or current["php_version"],
            }
        else:
            try:
                previous_php = current.get("php_version") or DEFAULT_PHP_VERSION
                current = ensure_tool_paths_applied(
                    {
                        "filebrowser_path": fb,
                        "phpmyadmin_path": pma,
                        "php_version": php_ver,
                    },
                    restart_filebrowser=True,
                    reload_apache=True,
                )
                applied_php = ensure_php_version_applied(
                    php_ver,
                    restart_fpm=(php_ver != previous_php),
                )
                current = load_panel_settings()
                message = (
                    "تنظیمات ذخیره شد. "
                    f"FileBrowser: {fb} — phpMyAdmin: {pma} — PHP: {applied_php}"
                )
                if php_ver != previous_php:
                    message += " (PHP-FPM ری‌استارت شد)"
            except Exception as exc:  # noqa: BLE001
                err = f"اعمال تنظیمات ناموفق بود: {exc}"

    return set_gate_cookie(
        app.make_response(
            render_template(
                "settings.html",
                settings=current,
                defaults=default_panel_settings(),
                php_versions=php_choices,
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
