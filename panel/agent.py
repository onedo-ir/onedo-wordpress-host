#!/usr/bin/env python3
"""OpenAI-compatible coding agent scoped to the WordPress document root."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

# Same path as panel/app.py WP_ROOT — site files live here in the container.
WORKSPACE = Path("/var/www/html")
SKILLS_DIR = Path(__file__).resolve().parent / "skills"
DEFAULT_SKILL = "wordpress-workspace"

MAX_LIST_ENTRIES = 400
MAX_READ_BYTES = 200_000
MAX_WRITE_BYTES = 1_000_000
MAX_GREP_MATCHES = 80
MAX_GREP_FILE_BYTES = 1_000_000
MAX_AGENT_STEPS = 24
MAX_HISTORY_MESSAGES = 40
HTTP_TIMEOUT = 120
MAX_COMMAND_CHARS = 8_000
MAX_COMMAND_OUTPUT = 50_000
DEFAULT_COMMAND_TIMEOUT = 60
MAX_COMMAND_TIMEOUT = 180
_COMMAND_SECRET_ENV = (
    "OPENAI_API_KEY",
    "PANEL_PASS",
    "PANEL_SECRET",
    "PANEL_GATE_TOKEN",
)


class AgentCancelled(Exception):
    """The panel asked this turn to stop before the next model or tool step."""


TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories under a relative path in the WordPress root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative directory path; empty = workspace root.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file. Use offset/limit for large files (1-based line numbers).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path."},
                    "offset": {
                        "type": "integer",
                        "description": "Start line (1-based). Optional.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of lines. Optional.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a text file with full content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact substring in a file (old_string → new_string).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {
                        "type": "boolean",
                        "description": "If true, replace every occurrence.",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a single file (not a directory).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": "Find files by glob pattern relative to workspace (e.g. wp-content/themes/**/*.php).",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {
                        "type": "string",
                        "description": "Optional subdirectory to search under.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_files",
            "description": "Search file contents with a regex. Optional glob filter (e.g. *.php).",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {
                        "type": "string",
                        "description": "Relative file or directory to search.",
                    },
                    "glob": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a shell command in the WordPress document root. "
                "Returns stdout, stderr, and the exit code. "
                "Use for wp-cli, php, composer, and checks. "
                "Read and edit files with the file tools, not the shell."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command, for example: wp plugin list",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Optional directory relative to the WordPress root. Empty means the root.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Seconds before the command is stopped. Default 60. Maximum 180.",
                    },
                },
                "required": ["command"],
            },
        },
    },
]


def agent_config() -> dict[str, str]:
    base = (os.environ.get("OPENAI_BASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    model = (os.environ.get("OPENAI_MODEL") or "gpt-4o-mini").strip() or "gpt-4o-mini"
    return {"base_url": base, "api_key": key, "model": model}


def agent_configured() -> bool:
    cfg = agent_config()
    return bool(cfg["base_url"] and cfg["api_key"])


def load_skill_text(name: str = DEFAULT_SKILL) -> str:
    path = SKILLS_DIR / name / "SKILL.md"
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except OSError:
        pass
    return (
        "You are a coding agent for a WordPress site. "
        "Work only inside the document root using the provided tools."
    )


def build_system_prompt() -> str:
    skill = load_skill_text()
    return (
        f"{skill.strip()}\n\n"
        "## Runtime\n\n"
        f"- Workspace absolute path: `{WORKSPACE}` (you can list, read, edit, and run commands in it now).\n"
        "- Tool paths are relative to that workspace. Empty path or `.` is the root.\n"
        "- The tools are already enabled. Do not tell the user that list_dir, read_file, or run_command are missing.\n"
        "- Inspect with list_dir / glob_files / grep_files / read_file before edits.\n"
        "- A file changes only after write_file or edit_file succeeds.\n"
        "- Shell commands run only through run_command. The working directory stays inside the WordPress root.\n"
        "- A command has run only after run_command returns. Include the exit code when it matters.\n"
        "- After finishing, give a short final answer (no raw tool dumps, no ```tool blocks).\n"
    )


def _normalize_user_path(rel: str | None) -> str:
    raw = re.sub(r"/{2,}", "/", (rel or "").strip().replace("\\", "/"))
    while raw.startswith("./"):
        raw = raw[2:]
    if raw in ("", "."):
        return ""
    roots = []
    roots.append(str(WORKSPACE).replace("\\", "/").rstrip("/"))
    try:
        roots.append(str(WORKSPACE.resolve()).replace("\\", "/").rstrip("/"))
    except OSError:
        pass
    for root in roots:
        if raw == root:
            return ""
        prefix = root + "/"
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    raw = raw.strip("/") if not raw.startswith("/") else raw
    if raw.startswith("/"):
        raise ValueError("مسیر باید نسبی باشد یا زیر ورک‌اسپیس وردپرس.")
    return raw.strip("/")


def _safe_resolve(rel: str | None, *, must_exist: bool = False) -> Path:
    raw = _normalize_user_path(rel)
    if ".." in Path(raw).parts:
        raise ValueError("مسیر خارج از ورک‌اسپیس مجاز نیست.")
    root = WORKSPACE.resolve()
    target = (root / raw).resolve() if raw else root
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("مسیر خارج از ورک‌اسپیس مجاز نیست.") from exc
    if must_exist and not target.exists():
        raise FileNotFoundError(f"پیدا نشد: {raw or '.'}")
    return target


def _rel_display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(WORKSPACE.resolve())).replace("\\", "/") or "."
    except ValueError:
        return path.name


def tool_list_dir(path: str = "") -> str:
    target = _safe_resolve(path)
    if not target.exists():
        raise FileNotFoundError(f"پوشه پیدا نشد: {path or '.'}")
    if not target.is_dir():
        raise ValueError("مسیر یک پوشه نیست.")
    entries: list[str] = []
    try:
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        raise RuntimeError(f"خواندن پوشه ممکن نشد: {exc}") from exc
    for child in children[:MAX_LIST_ENTRIES]:
        mark = "/" if child.is_dir() else ""
        entries.append(f"{child.name}{mark}")
    extra = ""
    if len(children) > MAX_LIST_ENTRIES:
        extra = f"\n… و {len(children) - MAX_LIST_ENTRIES} مورد دیگر"
    header = f"محتوای `{_rel_display(target)}`:"
    return header + "\n" + "\n".join(entries) + extra if entries else header + "\n(خالی)"


def tool_read_file(path: str, offset: int | None = None, limit: int | None = None) -> str:
    target = _safe_resolve(path, must_exist=True)
    if not target.is_file():
        raise ValueError("مسیر یک فایل نیست.")
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"خواندن فایل ممکن نشد: {exc}") from exc
    if len(data) > MAX_READ_BYTES and offset is None and limit is None:
        text = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")
        return (
            f"`{_rel_display(target)}` بزرگ است ({len(data)} بایت). "
            f"فقط {MAX_READ_BYTES} بایت اول:\n{text}"
        )
    text = data.decode("utf-8", errors="replace")
    if offset is None and limit is None:
        return text
    lines = text.splitlines()
    start = 1 if offset is None else max(1, int(offset))
    if start > len(lines):
        return f"(فایل {_rel_display(target)} فقط {len(lines)} خط دارد)"
    end = len(lines) + 1 if limit is None else start + max(0, int(limit))
    chunk = lines[start - 1 : end - 1]
    numbered = [f"{i}|{line}" for i, line in enumerate(chunk, start=start)]
    return "\n".join(numbered)


def _publish_web_path(path: Path) -> None:
    """Keep PHP able to read and update files the agent writes."""
    if not path.exists():
        return
    try:
        import grp
        import pwd

        uid = pwd.getpwnam("www-data").pw_uid
        gid = grp.getgrnam("www-data").gr_gid
    except (ImportError, KeyError, OSError):
        return
    try:
        os.chown(path, uid, gid)
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    except OSError:
        return


def _publish_written(path: Path) -> None:
    root = WORKSPACE.resolve()
    current = path.parent
    chain: list[Path] = []
    while True:
        try:
            current.relative_to(root)
        except ValueError:
            break
        if current == root:
            break
        chain.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(chain):
        _publish_web_path(directory)
    _publish_web_path(path)


def tool_write_file(path: str, content: str) -> str:
    if content is None:
        raise ValueError("content لازم است.")
    raw = content if isinstance(content, str) else str(content)
    encoded = raw.encode("utf-8")
    if len(encoded) > MAX_WRITE_BYTES:
        raise ValueError(f"حجم فایل از {MAX_WRITE_BYTES} بایت بیشتر است.")
    target = _safe_resolve(path)
    if target.exists() and target.is_dir():
        raise ValueError("نمی‌توان روی پوشه نوشت.")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(raw, encoding="utf-8")
        _publish_written(target)
    except OSError as exc:
        raise RuntimeError(f"نوشتن فایل ممکن نشد: {exc}") from exc
    return f"نوشته شد: `{_rel_display(target)}` ({len(encoded)} بایت)"


def tool_edit_file(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    if old_string == "":
        raise ValueError("old_string خالی مجاز نیست.")
    target = _safe_resolve(path, must_exist=True)
    if not target.is_file():
        raise ValueError("مسیر یک فایل نیست.")
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"خواندن فایل ممکن نشد: {exc}") from exc
    count = text.count(old_string)
    if count == 0:
        raise ValueError("old_string در فایل پیدا نشد.")
    if count > 1 and not replace_all:
        raise ValueError(
            f"old_string {count} بار تکرار شده؛ replace_all=true بگذارید یا متن یکتا بدهید."
        )
    if replace_all:
        updated = text.replace(old_string, new_string)
        n = count
    else:
        updated = text.replace(old_string, new_string, 1)
        n = 1
    encoded = updated.encode("utf-8")
    if len(encoded) > MAX_WRITE_BYTES:
        raise ValueError(f"حجم فایل بعد از ویرایش از {MAX_WRITE_BYTES} بایت بیشتر است.")
    try:
        target.write_text(updated, encoding="utf-8")
        _publish_written(target)
    except OSError as exc:
        raise RuntimeError(f"نوشتن فایل ممکن نشد: {exc}") from exc
    return f"ویرایش شد: `{_rel_display(target)}` ({n} جایگزینی)"


def tool_delete_file(path: str) -> str:
    target = _safe_resolve(path, must_exist=True)
    if not target.is_file():
        raise ValueError("فقط فایل قابل حذف است (نه پوشه).")
    try:
        target.unlink()
    except OSError as exc:
        raise RuntimeError(f"حذف ممکن نشد: {exc}") from exc
    return f"حذف شد: `{_rel_display(target)}`"


def tool_glob_files(pattern: str, path: str = "") -> str:
    pat = (pattern or "").strip()
    if not pat:
        raise ValueError("pattern لازم است.")
    base = _safe_resolve(path)
    if not base.exists():
        raise FileNotFoundError(f"مسیر پیدا نشد: {path or '.'}")
    if base.is_file():
        rel = _rel_display(base)
        return rel if fnmatch(base.name, pat) or fnmatch(rel, pat) else "(نتیجه‌ای نبود)"
    matches: list[str] = []
    # Support ** by walking and fnmatch on relative paths
    for root, _dirs, files in os.walk(base):
        root_path = Path(root)
        for name in files:
            full = root_path / name
            try:
                rel = str(full.resolve().relative_to(WORKSPACE.resolve())).replace("\\", "/")
            except ValueError:
                continue
            # Match against full relative path and basename
            if fnmatch(rel, pat) or fnmatch(name, pat) or fnmatch(rel, "**/" + pat):
                matches.append(rel)
            if len(matches) >= MAX_LIST_ENTRIES:
                break
        if len(matches) >= MAX_LIST_ENTRIES:
            break
    if not matches:
        return "(نتیجه‌ای نبود)"
    extra = ""
    if len(matches) >= MAX_LIST_ENTRIES:
        extra = f"\n… محدود به {MAX_LIST_ENTRIES} نتیجه"
    return "\n".join(matches) + extra


def tool_grep_files(pattern: str, path: str = "", glob: str | None = None) -> str:
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"regex نامعتبر: {exc}") from exc
    base = _safe_resolve(path or "")
    if not base.exists():
        raise FileNotFoundError(f"مسیر پیدا نشد: {path or '.'}")

    files: list[Path] = []
    if base.is_file():
        files = [base]
    else:
        for root, _dirs, names in os.walk(base):
            for name in names:
                if glob and not fnmatch(name, glob):
                    continue
                files.append(Path(root) / name)
                if len(files) > 5000:
                    break
            if len(files) > 5000:
                break

    hits: list[str] = []
    for fp in files:
        try:
            if fp.stat().st_size > MAX_GREP_FILE_BYTES:
                continue
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = _rel_display(fp)
        for i, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                snippet = line.strip()
                if len(snippet) > 200:
                    snippet = snippet[:200] + "…"
                hits.append(f"{rel}:{i}: {snippet}")
                if len(hits) >= MAX_GREP_MATCHES:
                    return "\n".join(hits) + f"\n… محدود به {MAX_GREP_MATCHES} نتیجه"
    return "\n".join(hits) if hits else "(نتیجه‌ای نبود)"


def _command_timeout(value: Any) -> int:
    if value is None or value == "":
        return DEFAULT_COMMAND_TIMEOUT
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("مهلت باید عدد صحیح ثانیه باشد.")
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("مهلت باید عدد صحیح ثانیه باشد.") from exc
    if seconds < 1 or seconds > MAX_COMMAND_TIMEOUT:
        raise ValueError(f"مهلت باید بین ۱ و {MAX_COMMAND_TIMEOUT} ثانیه باشد.")
    return seconds


def _command_child_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in _COMMAND_SECRET_ENV:
        env.pop(key, None)
    return env


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            return
    try:
        proc.wait(timeout=1)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            return
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _read_capped(stream: Any, chunks: list[str], overflow: threading.Event) -> None:
    total = 0
    try:
        while total < MAX_COMMAND_OUTPUT:
            piece = stream.read(4096)
            if not piece:
                return
            room = MAX_COMMAND_OUTPUT - total
            chunks.append(piece[:room])
            total += len(piece)
            if total >= MAX_COMMAND_OUTPUT:
                overflow.set()
                return
    except Exception:
        overflow.set()
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _format_command_result(
    command: str,
    cwd: str,
    code: int | None,
    stdout: str,
    stderr: str,
    *,
    timed_out: bool,
    truncated: bool,
    seconds: int,
) -> str:
    lines = [
        f"$ {command}",
        f"cwd: {cwd or '.'}",
        f"exit_code: {code if code is not None else 'نامشخص'}",
        "stdout:\n" + (stdout.rstrip() or "(خالی)"),
    ]
    if stderr.strip():
        lines.append("stderr:\n" + stderr.rstrip())
    if truncated:
        lines.append(f"… خروجی به {MAX_COMMAND_OUTPUT} نویسه محدود شد.")
    if timed_out:
        lines.append(f"دستور بعد از {seconds} ثانیه متوقف شد.")
    return "\n".join(lines)


def tool_run_command(
    command: str,
    timeout: Any = None,
    cwd: str = "",
    cancelled: threading.Event | None = None,
) -> str:
    if not isinstance(command, str):
        raise ValueError("دستور باید متن باشد.")
    if not isinstance(cwd, str):
        raise ValueError("پوشهٔ کار باید متن باشد.")
    cmd = command.strip()
    if not cmd:
        raise ValueError("دستور خالی است.")
    if len(cmd) > MAX_COMMAND_CHARS:
        raise ValueError("دستور خیلی بلند است.")
    seconds = _command_timeout(timeout)
    workdir = _safe_resolve(cwd or "")
    if not workdir.is_dir():
        raise NotADirectoryError(f"پوشهٔ کار پیدا نشد: {cwd or '.'}")
    if _stop_requested(cancelled):
        raise AgentCancelled()

    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=str(workdir),
        env=_command_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    stdout = proc.stdout
    stderr = proc.stderr
    if stdout is None or stderr is None:
        _kill_process_group(proc)
        raise RuntimeError("خروجی دستور در دسترس نیست.")

    out_chunks: list[str] = []
    err_chunks: list[str] = []
    overflow = threading.Event()
    readers = [
        threading.Thread(target=_read_capped, args=(stdout, out_chunks, overflow), name="agent-cmd-out", daemon=True),
        threading.Thread(target=_read_capped, args=(stderr, err_chunks, overflow), name="agent-cmd-err", daemon=True),
    ]
    started: list[threading.Thread] = []
    deadline = time.monotonic() + seconds
    timed_out = False
    stop = False
    try:
        for reader in readers:
            reader.start()
            started.append(reader)
        while proc.poll() is None:
            if _stop_requested(cancelled):
                stop = True
                _kill_process_group(proc)
                break
            if overflow.is_set():
                _kill_process_group(proc)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _kill_process_group(proc)
                break
            try:
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                continue
    finally:
        if proc.poll() is None:
            _kill_process_group(proc)
        for reader in started:
            reader.join(timeout=1)
        if any(reader.is_alive() for reader in started):
            overflow.set()

    if stop or _stop_requested(cancelled):
        raise AgentCancelled()

    rendered = _format_command_result(
        cmd,
        _rel_display(workdir),
        proc.returncode,
        "".join(out_chunks),
        "".join(err_chunks),
        timed_out=timed_out,
        truncated=overflow.is_set(),
        seconds=seconds,
    )
    if timed_out or overflow.is_set() or proc.returncode not in (0,):
        raise RuntimeError(rendered)
    return rendered


TOOL_HANDLERS = {
    "list_dir": lambda args: tool_list_dir(args.get("path") or ""),
    "read_file": lambda args: tool_read_file(
        args.get("path") or "",
        args.get("offset"),
        args.get("limit"),
    ),
    "write_file": lambda args: tool_write_file(args.get("path") or "", args.get("content") or ""),
    "edit_file": lambda args: tool_edit_file(
        args.get("path") or "",
        args.get("old_string") or "",
        args.get("new_string") if args.get("new_string") is not None else "",
        bool(args.get("replace_all")),
    ),
    "delete_file": lambda args: tool_delete_file(args.get("path") or ""),
    "glob_files": lambda args: tool_glob_files(args.get("pattern") or "", args.get("path") or ""),
    "grep_files": lambda args: tool_grep_files(
        args.get("pattern") or "",
        args.get("path") or "",
        args.get("glob"),
    ),
    "run_command": lambda args: tool_run_command(
        args.get("command") or "",
        args.get("timeout"),
        args.get("cwd") or "",
    ),
}


TOOL_ALIASES = {
    "project_list_dir": "list_dir",
    "list_directory": "list_dir",
    "listdir": "list_dir",
    "ls": "list_dir",
    "project_read_file": "read_file",
    "read": "read_file",
    "project_write_file": "write_file",
    "write": "write_file",
    "project_edit_file": "edit_file",
    "edit": "edit_file",
    "search_replace": "edit_file",
    "project_delete_file": "delete_file",
    "delete": "delete_file",
    "project_glob": "glob_files",
    "glob": "glob_files",
    "project_grep": "grep_files",
    "grep": "grep_files",
    "search": "grep_files",
    "shell": "run_command",
    "bash": "run_command",
    "exec": "run_command",
    "execute": "run_command",
    "terminal": "run_command",
    "run_shell": "run_command",
}


def run_tool(
    name: str,
    arguments: dict[str, Any] | str | None,
    cancelled: threading.Event | None = None,
) -> tuple[bool, str]:
    name = TOOL_ALIASES.get((name or "").strip(), (name or "").strip())
    if name not in TOOL_HANDLERS:
        return False, f"ابزار ناشناخته: {name}"
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return False, f"آرگومان JSON نامعتبر: {exc}"
    if not isinstance(arguments, dict):
        arguments = {}
    try:
        if name == "run_command":
            output = tool_run_command(
                arguments.get("command") or "",
                arguments.get("timeout"),
                arguments.get("cwd") or "",
                cancelled,
            )
        else:
            output = TOOL_HANDLERS[name](arguments)
        return True, str(output)
    except AgentCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 — surface tool errors to the model
        return False, f"خطا: {exc}"


def _normalize_history(history: list[Any] | None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(history, list):
        return out
    for item in history[-MAX_HISTORY_MESSAGES:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue
        out.append({"role": role, "content": text[:20_000]})
    return out


_NO_ACCESS_RE = re.compile(
    r"دسترسی ندارم|در دسترس نبود|ابزارهای خواندن فایل|cannot access|don't have access|do not have access|no access to",
    re.I,
)
_FILE_INTENT_RE = re.compile(
    r"فایل|پوشه|وردپرس|wp-content|wp-config|قالب|پلاگین|theme|plugin|لیست کن|ببین|ویرایش|ادیت",
    re.I,
)


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return ""


def _json_calls_from_blob(blob: str) -> list[dict[str, Any]]:
    blob = (blob or "").strip()
    if not blob:
        return []
    try:
        data: Any = json.loads(blob)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", blob)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    items = data if isinstance(data, list) else [data]
    calls: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("tool") or "").strip()
        args = item.get("arguments")
        if args is None:
            args = item.get("args")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        if name and not args:
            args = {
                key: value
                for key, value in item.items()
                if key not in ("name", "tool", "arguments", "args", "id")
            }
        if name:
            calls.append({"id": str(item.get("id") or name), "name": name, "arguments": args})
    return calls


def _text_tool_calls(content: str) -> list[dict[str, Any]]:
    if not content:
        return []
    found: list[dict[str, Any]] = []
    for match in re.finditer(r"```tool\s*([\s\S]*?)```", content, flags=re.I):
        found.extend(_json_calls_from_blob(match.group(1)))
    if not found:
        for match in re.finditer(r"<tool_call>\s*([\s\S]*?)</tool_call>", content, flags=re.I):
            found.extend(_json_calls_from_blob(match.group(1)))
    if not found:
        stripped = content.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            found.extend(_json_calls_from_blob(stripped))
    return found


def _native_tool_calls(msg: dict[str, Any]) -> list[dict[str, Any]]:
    raw_calls = msg.get("tool_calls")
    calls: list[dict[str, Any]] = []
    if isinstance(raw_calls, list):
        for call in raw_calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(fn.get("name") or call.get("name") or "").strip()
            args = fn.get("arguments")
            if args is None:
                args = call.get("arguments") if call.get("arguments") is not None else {}
            if not name:
                continue
            argument_text = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            calls.append(
                {
                    "id": str(call.get("id") or name),
                    "name": name,
                    "arguments": args,
                    "wire": {
                        "id": str(call.get("id") or name),
                        "type": "function",
                        "function": {"name": name, "arguments": argument_text},
                    },
                }
            )
    if calls:
        return calls
    legacy = msg.get("function_call")
    if isinstance(legacy, dict) and legacy.get("name"):
        legacy_args = legacy.get("arguments") if legacy.get("arguments") is not None else {}
        argument_text = (
            legacy_args if isinstance(legacy_args, str) else json.dumps(legacy_args, ensure_ascii=False)
        )
        return [
            {
                "id": "call_legacy",
                "name": str(legacy.get("name")),
                "arguments": legacy_args,
                "wire": {
                    "id": "call_legacy",
                    "type": "function",
                    "function": {"name": str(legacy.get("name")), "arguments": argument_text},
                },
            }
        ]
    return []


def _strip_tool_markup(text: str) -> str:
    cleaned = re.sub(r"```tool\s*[\s\S]*?```", "", text or "", flags=re.I)
    cleaned = re.sub(r"<tool_call>\s*[\s\S]*?</tool_call>", "", cleaned, flags=re.I)
    return cleaned.strip()


def _relax_tools(mode: dict[str, bool], detail: str) -> bool:
    low = (detail or "").lower()
    if not any(token in low for token in ("tool", "function")):
        return False
    if mode.get("native", True) and mode.get("choice", True):
        mode["choice"] = False
        return True
    if mode.get("native", True):
        mode["native"] = False
        mode["choice"] = False
        return True
    return False


def _stop_requested(cancelled: threading.Event | None) -> bool:
    return cancelled is not None and cancelled.is_set()


def _cancelled_payload(steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ok": False,
        "cancelled": True,
        "error": "کار ایجنت لغو شد.",
        "reply": "",
        "steps": steps,
        "configured": True,
    }


def _read_http(req: urllib.request.Request, cancelled: threading.Event | None) -> str:
    """Read a completion response. Stop waiting as soon as the turn is cancelled."""
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                box["raw"] = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 — re-raised on the caller thread
            box["exc"] = exc

    if _stop_requested(cancelled):
        raise AgentCancelled()
    if cancelled is None:
        work()
    else:
        thread = threading.Thread(target=work, name="agent-chat", daemon=True)
        thread.start()
        while thread.is_alive():
            thread.join(0.25)
            if _stop_requested(cancelled):
                raise AgentCancelled()
        if _stop_requested(cancelled):
            raise AgentCancelled()
    if "exc" in box:
        raise box["exc"]
    raw = box.get("raw")
    if not isinstance(raw, str):
        raise RuntimeError("پاسخ API خالی بود.")
    return raw


def _chat_completions(
    messages: list[dict[str, Any]],
    mode: dict[str, bool],
    cancelled: threading.Event | None = None,
) -> dict[str, Any]:
    cfg = agent_config()
    if not cfg["base_url"] or not cfg["api_key"]:
        raise RuntimeError(
            "OPENAI_BASE_URL و OPENAI_API_KEY در فایل .env تنظیم نشده‌اند."
        )
    url = f"{cfg['base_url']}/chat/completions"
    payload: dict[str, Any] = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0.2,
    }
    if mode.get("native", True):
        payload["tools"] = TOOL_DEFS
        if mode.get("choice", True):
            payload["tool_choice"] = "auto"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {cfg['api_key']}",
            "Accept": "application/json",
        },
    )
    try:
        raw = _read_http(req, cancelled)
    except AgentCancelled:
        raise
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        detail = err_body[:800] if err_body else str(exc)
        if _relax_tools(mode, detail):
            return _chat_completions(messages, mode, cancelled)
        raise RuntimeError(f"API خطا داد ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"اتصال به API برقرار نشد: {exc.reason}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("پاسخ API معتبر نبود.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("پاسخ API معتبر نبود.")
    err = data.get("error")
    if err:
        message = err.get("message") if isinstance(err, dict) else str(err)
        if _relax_tools(mode, str(message)):
            return _chat_completions(messages, mode, cancelled)
        raise RuntimeError(f"API خطا داد: {message}")
    return data


def _assistant_message(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("پاسخ مدل خالی بود.")
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(msg, dict):
        raise RuntimeError("پیام مدل نامعتبر بود.")
    return msg


def run_agent_turn(
    user_message: str,
    history: list[Any] | None = None,
    cancelled: threading.Event | None = None,
) -> dict[str, Any]:
    """Run one user turn with tool loop. Returns reply, steps, and errors."""
    text = (user_message or "").strip()
    if not text:
        return {"ok": False, "error": "پیام خالی است.", "reply": "", "steps": []}
    if len(text) > 20_000:
        return {"ok": False, "error": "پیام خیلی بلند است.", "reply": "", "steps": []}

    if not agent_configured():
        return {
            "ok": False,
            "error": "ایجنت تنظیم نشده: OPENAI_BASE_URL و OPENAI_API_KEY را در .env بگذارید و کانتینر را ری‌استارت کنید.",
            "reply": "",
            "steps": [],
            "configured": False,
        }

    if not WORKSPACE.is_dir():
        return {
            "ok": False,
            "error": f"ورک‌اسپیس وردپرس در دسترس نیست: {WORKSPACE}",
            "reply": "",
            "steps": [],
        }

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt()},
    ]
    messages.extend(_normalize_history(history))
    messages.append({"role": "user", "content": text})

    steps: list[dict[str, Any]] = []
    final_reply = ""
    mode = {"native": True, "choice": True}
    bootstrapped = False
    seen_listing = ""

    def remember_listing(name: str, ok: bool, result: str) -> None:
        nonlocal seen_listing
        if ok and name in ("list_dir", "ls") and not seen_listing:
            seen_listing = result[:8000]

    for _ in range(MAX_AGENT_STEPS):
        if _stop_requested(cancelled):
            return _cancelled_payload(steps)
        try:
            data = _chat_completions(messages, mode, cancelled)
        except AgentCancelled:
            return _cancelled_payload(steps)
        msg = _assistant_message(data)
        content = _message_text(msg.get("content")).strip()
        native_calls = _native_tool_calls(msg) if mode.get("native", True) else []
        calls = native_calls or _text_tool_calls(content)

        if calls:
            if native_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": content or "",
                        "tool_calls": [call["wire"] for call in native_calls],
                    }
                )
            else:
                messages.append({"role": "assistant", "content": content or ""})

            for call in calls:
                if _stop_requested(cancelled):
                    return _cancelled_payload(steps)
                try:
                    ok, result = run_tool(call["name"], call.get("arguments"), cancelled)
                except AgentCancelled:
                    return _cancelled_payload(steps)
                summary = result if len(result) <= 240 else result[:240] + "…"
                steps.append(
                    {
                        "type": "tool",
                        "name": call["name"],
                        "ok": ok,
                        "summary": summary,
                    }
                )
                remember_listing(call["name"], ok, result)
                clipped = result[:50_000]
                if native_calls:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "name": call["name"],
                            "content": clipped,
                        }
                    )
                else:
                    messages.append(
                        {
                            "role": "user",
                            "content": f"نتیجهٔ ابزار `{call['name']}`:\n{clipped}",
                        }
                    )
            final_reply = ""
            continue

        final_reply = content
        needs_files = bool(_NO_ACCESS_RE.search(content) or _FILE_INTENT_RE.search(text))
        if not bootstrapped and not steps and needs_files:
            if _stop_requested(cancelled):
                return _cancelled_payload(steps)
            bootstrapped = True
            try:
                listing = tool_list_dir("")
                listed_ok = True
            except Exception as exc:  # noqa: BLE001 — show the real filesystem error
                listing = f"خطا: {exc}"
                listed_ok = False
            summary = listing if len(listing) <= 240 else listing[:240] + "…"
            steps.append({"type": "tool", "name": "list_dir", "ok": listed_ok, "summary": summary})
            remember_listing("list_dir", listed_ok, listing)
            messages.append({"role": "assistant", "content": content or ""})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "ابزارهای فایل فعال‌اند. list_dir روی ریشهٔ وردپرس اجرا شد. "
                        "نگو به list_dir یا read_file دسترسی نداری.\n\n"
                        f"{listing[:50_000]}\n\n"
                        "با همین داده به کاربر جواب بده. برای خواندن یا ویرایش فایل "
                        "حتماً بلوک ```tool بفرست. توضیح متنی هیچ فایلی را عوض نمی‌کند."
                    ),
                }
            )
            final_reply = ""
            continue
        break
    else:
        if not final_reply:
            final_reply = "به سقف تعداد گام‌های ایجنت رسیدم. لطفاً درخواست را کوچک‌تر کنید یا دوباره بفرستید."

    final_reply = _strip_tool_markup(final_reply)
    edited = any(
        step.get("ok") and step.get("name") in ("edit_file", "write_file", "delete_file")
        for step in steps
    )
    if _NO_ACCESS_RE.search(final_reply or "") and seen_listing and not edited:
        final_reply = (
            "ریشهٔ وردپرس خوانده شد:\n\n"
            + seen_listing
            + "\n\nکدام فایل را باز یا ویرایش کنم؟"
        )
    if not final_reply:
        done = [
            step
            for step in steps
            if step.get("ok") and step.get("name") in ("edit_file", "write_file", "delete_file")
        ]
        if done:
            lines = [f"- {step['name']}: {step['summary']}" for step in done]
            final_reply = "تغییر اعمال شد:\n" + "\n".join(lines)
        elif seen_listing:
            final_reply = "ریشهٔ وردپرس:\n\n" + seen_listing
        else:
            final_reply = "انجام شد، اما مدل پاسخ متنی نداد."

    return {
        "ok": True,
        "reply": final_reply,
        "steps": steps,
        "error": None,
        "configured": True,
        "model": agent_config()["model"],
    }
