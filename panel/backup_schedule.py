#!/usr/bin/env python3
"""Scheduled WordPress/DB backups stored under /config/backups."""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path

BACKUP_ROOT = Path("/config/backups")
FULL_DIR = BACKUP_ROOT / "full"
DB_DIR = BACKUP_ROOT / "db"
SETTINGS_PATH = Path("/config/backup-settings.json")
STATE_PATH = Path("/config/backup-state.json")

FULL_NAME_RE = re.compile(r"^full-\d{8}-\d{6}\.tar\.gz$")
DB_NAME_RE = re.compile(r"^db-\d{8}-\d{6}\.sql\.gz$")

WEEKDAY_LABELS = (
    (0, "دوشنبه"),
    (1, "سه‌شنبه"),
    (2, "چهارشنبه"),
    (3, "پنج‌شنبه"),
    (4, "جمعه"),
    (5, "شنبه"),
    (6, "یکشنبه"),
)


def default_backup_settings() -> dict:
    return {
        "full_enabled": False,
        "full_frequency": "daily",  # daily | weekly
        "full_hour": 3,
        "full_minute": 0,
        "full_weekday": 0,  # Monday=0 … Sunday=6 (datetime.weekday)
        "full_retention_days": 7,
        "db_enabled": False,
        "db_minute": 5,
        "db_retention_days": 2,
    }


def _clamp_int(value, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def normalize_backup_settings(raw: dict | None) -> dict:
    base = default_backup_settings()
    if not isinstance(raw, dict):
        return base
    freq = str(raw.get("full_frequency") or base["full_frequency"]).strip().lower()
    if freq not in ("daily", "weekly"):
        freq = "daily"
    return {
        "full_enabled": bool(raw.get("full_enabled")),
        "full_frequency": freq,
        "full_hour": _clamp_int(raw.get("full_hour"), 0, 23, base["full_hour"]),
        "full_minute": _clamp_int(raw.get("full_minute"), 0, 59, base["full_minute"]),
        "full_weekday": _clamp_int(raw.get("full_weekday"), 0, 6, base["full_weekday"]),
        "full_retention_days": _clamp_int(
            raw.get("full_retention_days"), 1, 365, base["full_retention_days"]
        ),
        "db_enabled": bool(raw.get("db_enabled")),
        "db_minute": _clamp_int(raw.get("db_minute"), 0, 59, base["db_minute"]),
        "db_retention_days": _clamp_int(
            raw.get("db_retention_days"), 1, 90, base["db_retention_days"]
        ),
    }


def load_backup_settings() -> dict:
    try:
        if SETTINGS_PATH.is_file():
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            return normalize_backup_settings(data if isinstance(data, dict) else None)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return default_backup_settings()


def save_backup_settings(settings: dict) -> dict:
    previous = load_backup_settings()
    normalized = normalize_backup_settings(settings)
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    ensure_backup_dirs()
    # When enabling a schedule, skip catch-up for the current period.
    now = datetime.now()
    state = load_backup_state()
    if normalized["full_enabled"] and not previous["full_enabled"]:
        state["last_full_key"] = _full_due_key(now, normalized)
    if normalized["db_enabled"] and not previous["db_enabled"]:
        state["last_db_key"] = _db_due_key(now)
    save_backup_state(state)
    return normalized


def load_backup_state() -> dict:
    try:
        if STATE_PATH.is_file():
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    "last_full_key": str(data.get("last_full_key") or ""),
                    "last_db_key": str(data.get("last_db_key") or ""),
                }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return {"last_full_key": "", "last_db_key": ""}


def save_backup_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_full_key": str(state.get("last_full_key") or ""),
        "last_db_key": str(state.get("last_db_key") or ""),
    }
    STATE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def ensure_backup_dirs() -> None:
    FULL_DIR.mkdir(parents=True, exist_ok=True)
    DB_DIR.mkdir(parents=True, exist_ok=True)


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def human_bytes(num: int | float) -> str:
    value = float(max(0, num))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _avg_backup_size(directory: Path, pattern: str, fallback: int) -> int:
    sizes: list[int] = []
    if directory.is_dir():
        for path in directory.glob(pattern):
            try:
                sizes.append(path.stat().st_size)
            except OSError:
                continue
    if sizes:
        return max(1, int(sum(sizes) / len(sizes)))
    return max(1, fallback)


def estimate_full_unit_size() -> int:
    from app import WP_ROOT  # lazy: avoid import cycle at module load

    fallback = max(20 * 1024 * 1024, int(_dir_size(WP_ROOT) * 0.35) + 5 * 1024 * 1024)
    return _avg_backup_size(FULL_DIR, "full-*.tar.gz", fallback)


def estimate_db_unit_size() -> int:
    return _avg_backup_size(DB_DIR, "db-*.sql.gz", 8 * 1024 * 1024)


def expected_full_count(settings: dict) -> int:
    days = int(settings["full_retention_days"])
    if settings["full_frequency"] == "weekly":
        return max(1, (days + 6) // 7)
    return max(1, days)


def expected_db_count(settings: dict) -> int:
    return max(1, int(settings["db_retention_days"]) * 24)


def assess_backup_storage_risk(settings: dict | None = None) -> dict:
    """Estimate retention footprint vs free disk; return warnings for the UI."""
    cfg = normalize_backup_settings(settings or load_backup_settings())
    ensure_backup_dirs()
    try:
        usage = shutil.disk_usage(str(BACKUP_ROOT))
        free = int(usage.free)
        total = int(usage.total)
    except OSError:
        free, total = 0, 0

    full_unit = estimate_full_unit_size()
    db_unit = estimate_db_unit_size()
    full_n = expected_full_count(cfg) if cfg["full_enabled"] else 0
    db_n = expected_db_count(cfg) if cfg["db_enabled"] else 0
    estimated = full_n * full_unit + db_n * db_unit

    warnings: list[str] = []
    level = "ok"  # ok | warn | danger

    if cfg["db_enabled"] and cfg["db_retention_days"] >= 7:
        warnings.append(
            f"بکاپ ساعتی دیتابیس با نگهداری {cfg['db_retention_days']} روز حدوداً "
            f"{db_n} فایل می‌سازد ({human_bytes(db_n * db_unit)})."
        )
        level = "warn"
    if cfg["full_enabled"] and cfg["full_frequency"] == "daily" and cfg["full_retention_days"] >= 21:
        warnings.append(
            f"بکاپ کامل روزانه با نگهداری {cfg['full_retention_days']} روز فضای زیادی می‌گیرد "
            f"(حدود {human_bytes(full_n * full_unit)})."
        )
        level = "warn"
    if free and estimated > int(free * 0.5):
        warnings.append(
            f"حجم تقریبی بکاپ‌های نگهداری‌شده ({human_bytes(estimated)}) بیش از نیمی از "
            f"فضای آزاد دیسک ({human_bytes(free)}) است و ممکن است سرور پر شود."
        )
        level = "warn"
    if free and estimated > int(free * 0.85):
        warnings.append(
            f"خطر پر شدن دیسک: برآورد نگهداری ({human_bytes(estimated)}) نزدیک یا بیش از "
            f"فضای آزاد ({human_bytes(free)}) است. تعداد روز نگهداری را کم کنید."
        )
        level = "danger"
    if free and free < 512 * 1024 * 1024 and (cfg["full_enabled"] or cfg["db_enabled"]):
        warnings.append(
            f"فضای آزاد دیسک کم است ({human_bytes(free)}). قبل از فعال‌سازی زمان‌بندی، فضا آزاد کنید."
        )
        level = "danger" if level != "ok" else "warn"

    return {
        "level": level,
        "warnings": warnings,
        "estimated_bytes": estimated,
        "estimated_human": human_bytes(estimated),
        "free_bytes": free,
        "free_human": human_bytes(free) if free else "نامشخص",
        "total_bytes": total,
        "full_unit_human": human_bytes(full_unit),
        "db_unit_human": human_bytes(db_unit),
        "full_count": full_n,
        "db_count": db_n,
    }


def parse_backup_settings_form(form) -> tuple[dict, str | None]:
    """Parse request.form into settings; return (settings, error)."""
    frequency = (form.get("full_frequency") or "daily").strip().lower()
    if frequency not in ("daily", "weekly"):
        return default_backup_settings(), "دورهٔ بکاپ کامل نامعتبر است."

    settings = normalize_backup_settings(
        {
            "full_enabled": form.get("full_enabled") == "1",
            "full_frequency": frequency,
            "full_hour": form.get("full_hour"),
            "full_minute": form.get("full_minute"),
            "full_weekday": form.get("full_weekday"),
            "full_retention_days": form.get("full_retention_days"),
            "db_enabled": form.get("db_enabled") == "1",
            "db_minute": form.get("db_minute"),
            "db_retention_days": form.get("db_retention_days"),
        }
    )
    return settings, None


def list_stored_backups(kind: str) -> list[dict]:
    ensure_backup_dirs()
    if kind == "full":
        directory, pattern, name_re = FULL_DIR, "full-*.tar.gz", FULL_NAME_RE
    elif kind == "db":
        directory, pattern, name_re = DB_DIR, "db-*.sql.gz", DB_NAME_RE
    else:
        return []

    items: list[dict] = []
    for path in directory.glob(pattern):
        if not path.is_file() or not name_re.match(path.name):
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        items.append(
            {
                "name": path.name,
                "kind": kind,
                "size": st.st_size,
                "size_human": human_bytes(st.st_size),
                "mtime": st.st_mtime,
                "mtime_label": datetime.fromtimestamp(st.st_mtime).strftime(
                    "%Y-%m-%d %H:%M"
                ),
            }
        )
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def resolve_stored_backup(kind: str, name: str) -> Path | None:
    name = Path(name or "").name
    if kind == "full":
        if not FULL_NAME_RE.match(name):
            return None
        path = FULL_DIR / name
    elif kind == "db":
        if not DB_NAME_RE.match(name):
            return None
        path = DB_DIR / name
    else:
        return None
    try:
        resolved = path.resolve()
        root = (FULL_DIR if kind == "full" else DB_DIR).resolve()
        if resolved.parent != root or not resolved.is_file():
            return None
        return resolved
    except OSError:
        return None


def prune_stored_backups(kind: str, retention_days: int) -> int:
    ensure_backup_dirs()
    retention_days = _clamp_int(retention_days, 1, 365, 7)
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for item in list_stored_backups(kind):
        if item["mtime"] < cutoff:
            path = resolve_stored_backup(kind, item["name"])
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
    return removed


def create_stored_full_backup() -> Path:
    from app import create_backup_archive

    ensure_backup_dirs()
    archive = create_backup_archive()
    dest = FULL_DIR / f"full-{_stamp()}.tar.gz"
    try:
        shutil.move(str(archive), str(dest))
    except OSError:
        shutil.copy2(str(archive), str(dest))
        archive.unlink(missing_ok=True)
    settings = load_backup_settings()
    prune_stored_backups("full", settings["full_retention_days"])
    return dest


def create_stored_db_backup() -> Path:
    from app import dump_database

    ensure_backup_dirs()
    stamp = _stamp()
    dest = DB_DIR / f"db-{stamp}.sql.gz"
    fd, tmp_name = tempfile.mkstemp(prefix="dbdump-", suffix=".sql", dir=str(DB_DIR))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        dump_database(tmp)
        with tmp.open("rb") as src, gzip.open(dest, "wb") as out:
            shutil.copyfileobj(src, out, length=1024 * 1024)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    finally:
        tmp.unlink(missing_ok=True)
    settings = load_backup_settings()
    prune_stored_backups("db", settings["db_retention_days"])
    return dest


def restore_stored_full(name: str) -> None:
    from app import restore_from_archive

    path = resolve_stored_backup("full", name)
    if path is None:
        raise RuntimeError("فایل بکاپ کامل پیدا نشد.")
    restore_from_archive(path)


def restore_stored_db(name: str) -> None:
    from app import BACKUP_WORKDIR, import_database

    path = resolve_stored_backup("db", name)
    if path is None:
        raise RuntimeError("فایل بکاپ دیتابیس پیدا نشد.")
    BACKUP_WORKDIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix="dbrest-", suffix=".sql", dir=str(BACKUP_WORKDIR)
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with gzip.open(path, "rb") as src, tmp.open("wb") as out:
            shutil.copyfileobj(src, out, length=1024 * 1024)
        import_database(tmp)
    finally:
        tmp.unlink(missing_ok=True)


def delete_stored_backup(kind: str, name: str) -> None:
    path = resolve_stored_backup(kind, name)
    if path is None:
        raise RuntimeError("فایل بکاپ پیدا نشد.")
    path.unlink(missing_ok=True)


def _full_due_key(now: datetime, settings: dict) -> str:
    if settings["full_frequency"] == "weekly":
        # ISO week key anchored to scheduled weekday occurrence
        return f"W{now.strftime('%G-%V')}-{settings['full_weekday']}"
    return now.strftime("%Y-%m-%d")


def _db_due_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d-%H")


def _time_reached(now: datetime, hour: int, minute: int) -> bool:
    return (now.hour, now.minute) >= (hour, minute)


def due_jobs(now: datetime | None = None) -> list[str]:
    """Return job names that should run now: 'full' and/or 'db'."""
    settings = load_backup_settings()
    state = load_backup_state()
    now = now or datetime.now()
    due: list[str] = []

    if settings["full_enabled"]:
        ready = _time_reached(now, settings["full_hour"], settings["full_minute"])
        if settings["full_frequency"] == "weekly":
            ready = ready and now.weekday() == settings["full_weekday"]
        key = _full_due_key(now, settings)
        if ready and state.get("last_full_key") != key:
            due.append("full")

    if settings["db_enabled"]:
        if now.minute >= settings["db_minute"]:
            key = _db_due_key(now)
            if state.get("last_db_key") != key:
                due.append("db")

    return due


def mark_job_done(job: str, when: datetime | None = None) -> None:
    when = when or datetime.now()
    settings = load_backup_settings()
    state = load_backup_state()
    if job == "full":
        state["last_full_key"] = _full_due_key(when, settings)
    elif job == "db":
        state["last_db_key"] = _db_due_key(when)
    save_backup_state(state)


def run_due_jobs_once() -> list[str]:
    ran: list[str] = []
    for job in due_jobs():
        try:
            if job == "full":
                create_stored_full_backup()
            elif job == "db":
                create_stored_db_backup()
            else:
                continue
            mark_job_done(job)
            ran.append(job)
            print(f"[backup-scheduler] completed job={job}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[backup-scheduler] job={job} failed: {exc}", flush=True)
    return ran


def run_scheduler_loop(poll_seconds: int = 30) -> None:
    ensure_backup_dirs()
    print("[backup-scheduler] started", flush=True)
    while True:
        try:
            run_due_jobs_once()
        except Exception as exc:  # noqa: BLE001
            print(f"[backup-scheduler] loop error: {exc}", flush=True)
        time.sleep(max(10, int(poll_seconds)))


if __name__ == "__main__":
    run_scheduler_loop()
