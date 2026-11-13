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
        f"- Workspace absolute path: `{WORKSPACE}`\n"
        "- All tool paths are relative to that workspace.\n"
        "- Prefer inspecting with list_dir / glob_files / grep_files before edits.\n"
        "- After finishing, give a short final answer to the user (no raw tool dumps).\n"
    )


def _safe_resolve(rel: str | None, *, must_exist: bool = False) -> Path:
    raw = (rel or "").strip().replace("\\", "/")
    if raw.startswith("/"):
        raise ValueError("مسیر باید نسبی باشد (بدون / اول).")
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


def run_tool(name: str, arguments: dict[str, Any] | str | None) -> tuple[bool, str]:
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


def _chat_completions(messages: list[dict[str, Any]]) -> dict[str, Any]:
    cfg = agent_config()
    if not cfg["base_url"] or not cfg["api_key"]:
        raise RuntimeError(
            "OPENAI_BASE_URL و OPENAI_API_KEY در فایل .env تنظیم نشده‌اند."
        )
    url = f"{cfg['base_url']}/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "tools": TOOL_DEFS,
        "tool_choice": "auto",
        "temperature": 0.2,
    }
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
        raise RuntimeError(f"API خطا داد ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"اتصال به API برقرار نشد: {exc.reason}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("پاسخ API معتبر نبود.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("پاسخ API معتبر نبود.")
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

    for _ in range(MAX_AGENT_STEPS):
        data = _chat_completions(messages)
        msg = _assistant_message(data)
        tool_calls = msg.get("tool_calls")
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            final_reply = content.strip()

        if not tool_calls:
            break

        # Persist assistant tool_calls message for the provider protocol
        assistant_entry: dict[str, Any] = {"role": "assistant", "content": content or ""}
        if tool_calls:
            assistant_entry["tool_calls"] = tool_calls
        messages.append(assistant_entry)

        if not isinstance(tool_calls, list):
            break

        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(fn.get("name") or "")
            raw_args = fn.get("arguments") or "{}"
            ok, result = run_tool(name, raw_args)
            summary = result if len(result) <= 240 else result[:240] + "…"
            steps.append(
                {
                    "type": "tool",
                    "name": name,
                    "ok": ok,
                    "summary": summary,
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or name),
                    "content": result[:50_000],
                }
            )
    else:
        if not final_reply:
            final_reply = "به سقف تعداد گام‌های ایجنت رسیدم. لطفاً درخواست را کوچک‌تر کنید یا دوباره بفرستید."

    if not final_reply:
        final_reply = "انجام شد، اما مدل پاسخ متنی نداد."

    return {
        "ok": True,
        "reply": final_reply,
        "steps": steps,
        "error": None,
        "configured": True,
        "model": agent_config()["model"],
    }
