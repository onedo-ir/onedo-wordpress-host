#!/usr/bin/env python3
"""OpenAI-compatible coding agent scoped to the WordPress document root."""

from __future__ import annotations

import json
import os
import re
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
        f"- Workspace absolute path: `{WORKSPACE}` (you can list, read, and edit it now).\n"
        "- Tool paths are relative to that workspace. Empty path or `.` is the root.\n"
        "- The tools are already enabled. Do not tell the user that list_dir/read_file are missing.\n"
        "- Inspect with list_dir / glob_files / grep_files / read_file before edits.\n"
        "- A file changes only after write_file or edit_file succeeds.\n"
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
}


def run_tool(name: str, arguments: dict[str, Any] | str | None) -> tuple[bool, str]:
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
        return True, str(TOOL_HANDLERS[name](arguments))
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


def _chat_completions(messages: list[dict[str, Any]], mode: dict[str, bool]) -> dict[str, Any]:
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
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        detail = err_body[:800] if err_body else str(exc)
        if _relax_tools(mode, detail):
            return _chat_completions(messages, mode)
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
            return _chat_completions(messages, mode)
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


def run_agent_turn(user_message: str, history: list[Any] | None = None) -> dict[str, Any]:
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
        data = _chat_completions(messages, mode)
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
                ok, result = run_tool(call["name"], call.get("arguments"))
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
