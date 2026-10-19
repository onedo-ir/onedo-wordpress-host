#!/usr/bin/env python3
"""Admin panel at /panel — login, config edit, shell, links to tools."""

from __future__ import annotations

import hashlib
import hmac
import os
import subprocess
from functools import wraps
from glob import glob
from pathlib import Path

from flask import (
    Flask,
    redirect,
    render_template,
    request,
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

COOKIE_NAME = "panel_gate"
SHELL_TIMEOUT = 45
SHELL_MAX_OUTPUT = 200_000


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


@app.route("/panel/shell", methods=["GET", "POST"])
@login_required
def shell():
    output = ""
    command = ""
    if request.method == "POST":
        command = (request.form.get("command") or "").strip()
        if not command:
            output = "دستوری وارد نشده."
        elif len(command) > 4000:
            output = "دستور خیلی طولانی است."
        else:
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
                parts.append(f"\n[exit {proc.returncode}]")
                output = "".join(parts)
            except subprocess.TimeoutExpired:
                output = f"زمان اجرا از {SHELL_TIMEOUT} ثانیه بیشتر شد؛ فرایند قطع شد."
            except Exception as exc:  # noqa: BLE001
                output = f"خطا: {exc}"
            if len(output) > SHELL_MAX_OUTPUT:
                output = output[:SHELL_MAX_OUTPUT] + "\n… [خروجی کوتاه شد]"

    return set_gate_cookie(
        app.make_response(
            render_template("shell.html", command=command, output=output)
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
