#!/usr/bin/env python3
"""Admin panel at /panel — login, config edit, shell, backup, links to tools."""

from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
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
BACKUP_WORKDIR = Path("/tmp/panel-backups")
BACKUP_CREATE_TIMEOUT = 600
BACKUP_RESTORE_TIMEOUT = 900


def panel_user() -> str:
    return os.environ.get("PANEL_USER", "admin")


def panel_pass() -> str:
    return os.environ.get("PANEL_PASS", "")


def gate_token() -> str:
    """Cookie value Apache checks before allowing /filebrowser and /phpmyadmin."""
    explicit = os.environ.get("PANEL_GATE_TOKEN", "").strip()
    if explicit:
        return explicit
    raw = f"{panel_user()}:{panel_pass()}:{os.environ.get('PANEL_SECRET', 'wp-panel-gate')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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

        subprocess.run(
            ["chown", "-R", "www-data:www-data", str(WP_ROOT)],
            check=False,
            capture_output=True,
            timeout=120,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


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
        ok_user = hmac.compare_digest(user, panel_user())
        ok_pass = hmac.compare_digest(password, panel_pass()) if panel_pass() else False
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
    return set_gate_cookie(
        app.make_response(
            render_template(
                "dashboard.html",
                filebrowser_url=f"{host}/filebrowser",
                phpmyadmin_url=f"{host}/phpmyadmin",
                files=allowed_config_files(),
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


@app.route("/panel/backup", methods=["GET", "POST"])
@login_required
def backup():
    message = None
    err = None

    if request.method == "POST":
        action = request.form.get("action") or ""
        if action == "restore":
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

    return set_gate_cookie(
        app.make_response(
            render_template("backup.html", message=message, error=err)
        )
    )


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
        return set_gate_cookie(
            app.make_response(
                render_template(
                    "backup.html",
                    message=None,
                    error=f"ساخت بکاپ ناموفق: {exc}",
                )
            )
        )
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


# Avoid open redirects from crafted Host — links use request.host_url only for same host
@app.after_request
def security_headers(resp):
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=9090)
