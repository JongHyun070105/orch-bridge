#!/usr/bin/env python3
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

PROVIDER_DEFAULT_BINARIES = {
    "codex": "codex",
    "claude": "claude",
    "agy": "agy",
    "commandcode": "cmd",
}

PROVIDER_ENV_OVERRIDES = {
    "codex": ("ORCHBRIDGE_CODEX_BIN", "AI_ORCH_CODEX_BIN", "CODEX_CLI_PATH"),
    "claude": ("ORCHBRIDGE_CLAUDE_BIN", "AI_ORCH_CLAUDE_BIN", "CLAUDE_CLI_PATH", "CLAUDE_CODE_CLI_PATH"),
    "agy": ("ORCHBRIDGE_AGY_BIN", "AI_ORCH_AGY_BIN"),
    "commandcode": (
        "ORCHBRIDGE_COMMANDCODE_BIN",
        "AI_ORCH_CMD_BIN",
        "COMMANDCODE_CLI_PATH",
        "COMMAND_CODE_CLI_PATH",
        "CMD_CLI_PATH",
    ),
}

_POSITIVE_CACHE: dict[tuple[str, str, str, str], str] = {}


def provider_config(config: dict[str, Any], provider: str) -> dict[str, Any]:
    providers = config.get("providers", {}) if isinstance(config, dict) else {}
    raw = providers.get(provider, {}) if isinstance(providers, dict) else {}
    if isinstance(raw, bool):
        return {"enabled": raw, "binary": PROVIDER_DEFAULT_BINARIES[provider]}
    if not isinstance(raw, dict):
        raw = {}
    return raw


def provider_config_enabled(config: dict[str, Any], provider: str) -> bool:
    value = provider_config(config, provider).get("enabled", "auto")
    return not (value is False or str(value).strip().lower() in {"false", "off", "0", "disabled"})


def provider_binary(config: dict[str, Any], provider: str) -> str:
    item = provider_config(config, provider)
    return str(item.get("binary") or PROVIDER_DEFAULT_BINARIES[provider]).strip()


def executable_file(value: str | os.PathLike[str] | None) -> str | None:
    if not value:
        return None
    try:
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            return None
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
    except Exception:
        pass
    return None


def _shell_probe_args(shell: str, script: str, *, interactive: bool) -> list[str]:
    base = Path(shell).name.casefold()
    if base in {"zsh", "bash", "ksh"}:
        return [shell, "-lic" if interactive else "-lc", script]
    if base == "fish":
        args = [shell, "-l"]
        if interactive:
            args.append("-i")
        args += ["-c", script]
        return args
    return [shell, "-ic" if interactive else "-c", script]


def _probe_shell(shell: str, binary: str, *, interactive: bool, home: Path) -> str | None:
    if "/" in binary:
        return None
    quoted = shlex.quote(binary)
    script = (
        'p=""\n'
        'if [ -n "${ZSH_VERSION:-}" ]; then\n'
        f'  p="$(whence -p {quoted} 2>/dev/null || true)"\n'
        'fi\n'
        'if [ -z "$p" ]; then\n'
        f'  p="$(command -v {quoted} 2>/dev/null || true)"\n'
        'fi\n'
        'case "$p" in\n'
        '  /*) printf "__ORCHBRIDGE_CLI__=%s\\n" "$p" ;;\n'
        'esac\n'
    )
    try:
        result = subprocess.run(
            _shell_probe_args(shell, script, interactive=interactive),
            cwd=str(home),
            capture_output=True,
            text=True,
            timeout=12,
            env=os.environ.copy(),
        )
    except Exception:
        return None
    combined = "\n".join(x for x in (result.stdout, result.stderr) if x)
    for line in reversed(combined.splitlines()):
        if "__ORCHBRIDGE_CLI__=" not in line:
            continue
        found = executable_file(line.split("__ORCHBRIDGE_CLI__=", 1)[1].strip())
        if found:
            return found
    return None


def _resolve_candidate(value: str | None) -> str | None:
    if not value:
        return None
    direct = executable_file(value)
    if direct:
        return direct
    if "/" not in value:
        found = shutil.which(value)
        if found:
            return executable_file(found) or str(Path(found).expanduser().resolve())
    return None


def resolve_provider_cli(
    provider: str,
    config: dict[str, Any] | None = None,
    *,
    home: Path | None = None,
    use_cache: bool = True,
) -> str | None:
    """Resolve a provider CLI without caching misses.

    Search order: explicit environment override, configured binary, current PATH,
    common user/runtime paths, NVM version bins, then login + interactive shell PATH.
    Positive results may be cached; negative results are intentionally never cached.
    """
    if provider not in PROVIDER_DEFAULT_BINARIES:
        raise KeyError(provider)
    config = config or {}
    home = (home or Path.home()).expanduser().resolve()
    binary = provider_binary(config, provider)
    cache_key = (provider, binary, str(home), os.environ.get("PATH", ""))

    for key in PROVIDER_ENV_OVERRIDES[provider]:
        resolved = _resolve_candidate(os.environ.get(key))
        if resolved:
            if use_cache:
                _POSITIVE_CACHE[cache_key] = resolved
            return resolved

    if use_cache and cache_key in _POSITIVE_CACHE:
        cached = executable_file(_POSITIVE_CACHE[cache_key])
        if cached:
            return cached
        _POSITIVE_CACHE.pop(cache_key, None)

    resolved = _resolve_candidate(binary)
    if resolved:
        if use_cache:
            _POSITIVE_CACHE[cache_key] = resolved
        return resolved

    if "/" not in binary:
        fixed_dirs = (
            home / ".local/bin",
            home / ".codex/bin",
            home / ".npm-global/bin",
            home / ".bun/bin",
            home / ".volta/bin",
            home / ".cargo/bin",
            home / "Library/pnpm",
            Path("/opt/homebrew/bin"),
            Path("/usr/local/bin"),
            Path("/usr/bin"),
        )
        for base in fixed_dirs:
            resolved = executable_file(base / binary)
            if resolved:
                if use_cache:
                    _POSITIVE_CACHE[cache_key] = resolved
                return resolved

        try:
            nvm_candidates = sorted(
                (home / ".nvm/versions/node").glob(f"*/bin/{binary}"),
                key=lambda path: path.lstat().st_mtime if path.exists() or path.is_symlink() else 0,
                reverse=True,
            )
        except Exception:
            nvm_candidates = []
        for candidate in nvm_candidates:
            resolved = executable_file(candidate)
            if resolved:
                if use_cache:
                    _POSITIVE_CACHE[cache_key] = resolved
                return resolved

        shell = os.environ.get("SHELL", "").strip()
        if shell and Path(shell).is_file():
            for interactive in (False, True):
                resolved = _probe_shell(shell, binary, interactive=interactive, home=home)
                if resolved:
                    if use_cache:
                        _POSITIVE_CACHE[cache_key] = resolved
                    return resolved

    return None


def effective_provider_enabled(config: dict[str, Any], provider: str) -> bool:
    return provider_config_enabled(config, provider) and resolve_provider_cli(provider, config) is not None


def clear_positive_cache() -> None:
    _POSITIVE_CACHE.clear()
