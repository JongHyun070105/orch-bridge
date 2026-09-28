#!/usr/bin/env python3
from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any


def system_name() -> str:
    return platform.system()


def preferred_shell() -> str:
    raw = os.environ.get("SHELL", "").strip()
    if raw and Path(raw).exists():
        return raw
    for name in ("bash", "sh", "fish", "zsh"):
        found = shutil.which(name)
        if found:
            return found
    return "/bin/sh"


def login_shell_command() -> str:
    shell = preferred_shell()
    # All common shells accepted here support a login flag; if a niche shell
    # does not, users can override SHELL or ORCHBRIDGE_SHELL_COMMAND.
    override = os.environ.get("ORCHBRIDGE_SHELL_COMMAND", "").strip()
    if override:
        return override
    return f"exec {shlex.quote(shell)} -l"


def clipboard_backend() -> tuple[list[str] | None, str]:
    sysname = system_name()
    if sysname == "Darwin" and shutil.which("pbcopy"):
        return ["pbcopy"], "pbcopy"
    if sysname == "Linux":
        if shutil.which("wl-copy"):
            return ["wl-copy"], "wl-copy"
        if shutil.which("xclip"):
            return ["xclip", "-selection", "clipboard"], "xclip"
        if shutil.which("xsel"):
            return ["xsel", "--clipboard", "--input"], "xsel"
    return None, "unavailable"


def copy_text(text: str, *, timeout: int = 5) -> dict[str, Any]:
    argv, backend = clipboard_backend()
    if not argv:
        return {
            "ok": False,
            "backend": backend,
            "reason": "no clipboard backend found (macOS: pbcopy; Linux: wl-copy/xclip/xsel)",
        }
    try:
        p = subprocess.run(argv, input=text, text=True, capture_output=True, timeout=timeout)
    except Exception as e:
        return {"ok": False, "backend": backend, "reason": repr(e)}
    if p.returncode == 0:
        return {"ok": True, "backend": backend}
    return {
        "ok": False,
        "backend": backend,
        "reason": (p.stderr or p.stdout or f"exit={p.returncode}").strip(),
    }
