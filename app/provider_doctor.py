#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from provider_runtime import (
    provider_binary,
    provider_config,
    provider_config_enabled,
    resolve_provider_cli,
)

HOME = Path.home()
CONFIG_PATH = HOME / ".config/orchbridge/config.json"

PROVIDERS = (
    ("codex", "Codex", ("--version",)),
    ("claude", "Claude Code", ("--version",)),
    ("commandcode", "Command Code", ("--version",)),
    ("agy", "AGY", ("--version",)),
)

AUTH_RE = re.compile(r"not logged in|login required|authentication|oauth|sign in", re.I)
QUOTA_RE = re.compile(r"rate.?limit|quota|usage limit|credits? exhausted|insufficient credits", re.I)


def load_config() -> dict[str, Any]:
    try:
        data = json.loads(CONFIG_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _configured_path_state(binary: str) -> dict[str, Any] | None:
    if not binary or "/" not in binary:
        return None
    path = Path(binary).expanduser()
    if path.is_symlink() and not path.exists():
        return {"status": "BROKEN_INSTALL", "reason": "configured path is a broken symlink", "path": str(path)}
    if path.exists() and not os.access(path, os.X_OK):
        return {"status": "FOUND_BUT_NOT_EXECUTABLE", "reason": "configured path is not executable", "path": str(path)}
    if not path.exists():
        return {"status": "MISSING", "reason": "configured path does not exist", "path": str(path)}
    return None


def run_version(path: str, args: tuple[str, ...]) -> tuple[str, str]:
    try:
        proc = subprocess.run([path, *args], capture_output=True, text=True, timeout=12)
    except PermissionError as exc:
        return "FOUND_BUT_NOT_EXECUTABLE", f"PermissionError: {exc}"
    except Exception as exc:
        return "UNKNOWN", f"{type(exc).__name__}: {exc}"
    output = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
    first = output.splitlines()[0] if output else f"exit={proc.returncode}"
    if proc.returncode == 0:
        return "HEALTHY", first
    if AUTH_RE.search(output):
        return "AUTH_REQUIRED", first
    if QUOTA_RE.search(output):
        return "QUOTA_EXHAUSTED", first
    return "FOUND_BUT_COMMAND_FAILS", first


def _node_roots() -> list[Path]:
    roots: list[Path] = []
    nvm = HOME / ".nvm/versions/node"
    if nvm.exists():
        try:
            roots.extend(sorted((p for p in nvm.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True))
        except Exception:
            pass
    for root in (Path("/opt/homebrew"), Path("/usr/local"), HOME / ".npm-global"):
        if root.exists():
            roots.append(root)
    return roots


def codex_npm_state() -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    stale_dirs: list[str] = []
    for root in _node_roots():
        candidates = []
        if (root / "lib/node_modules").exists():
            candidates.append((root / "lib/node_modules/@openai", root / "bin/codex"))
        if (root / "node_modules").exists():
            candidates.append((root / "node_modules/@openai", root / "bin/codex"))
        for scope, link in candidates:
            package = scope / "codex"
            if scope.exists():
                try:
                    stale_dirs.extend(str(p) for p in sorted(scope.glob(".codex-*")) if ".broken-" not in p.name)
                except Exception:
                    pass
            if package.exists() or link.exists() or link.is_symlink():
                observations.append({
                    "root": str(root),
                    "package_path": str(package) if package.exists() else None,
                    "installed_package": package.exists(),
                    "bin_path": str(link) if link.exists() or link.is_symlink() else None,
                    "bin_is_symlink": link.is_symlink(),
                    "bin_broken_symlink": bool(link.is_symlink() and not link.exists()),
                    "bin_executable": bool(link.exists() and os.access(link, os.X_OK)),
                })
    return {"observations": observations, "stale_dirs": sorted(set(stale_dirs))}


def collect() -> dict[str, Any]:
    config = load_config()
    result: dict[str, Any] = {"providers": {}, "tools": {}, "codex_npm": {}, "warnings": []}
    npm = codex_npm_state()
    result["codex_npm"] = npm

    for key, label, args in PROVIDERS:
        item_cfg = provider_config(config, key)
        binary = provider_binary(config, key)
        item: dict[str, Any] = {
            "label": label,
            "configured": item_cfg.get("enabled", "auto"),
            "binary": binary,
            "status": "UNKNOWN",
            "ok": False,
        }
        if not provider_config_enabled(config, key):
            item.update({"status": "DISABLED", "reason": "disabled by configuration"})
            result["providers"][key] = item
            continue

        configured_state = _configured_path_state(binary)
        path = resolve_provider_cli(key, config, home=HOME, use_cache=False)
        if path:
            status, detail = run_version(path, args)
            item.update({"status": status, "ok": status == "HEALTHY", "path": path, "version": detail})
        elif configured_state:
            item.update(configured_state)
        else:
            item.update({"status": "MISSING", "reason": "executable not found"})

        if key == "codex" and item["status"] == "MISSING":
            broken = [
                x for x in npm["observations"]
                if x.get("installed_package")
                and (not x.get("bin_path") or x.get("bin_broken_symlink") or not x.get("bin_executable"))
            ]
            if broken:
                item.update({
                    "status": "BROKEN_INSTALL",
                    "reason": "@openai/codex package exists but executable link is missing/broken",
                })
        result["providers"][key] = item

    for name in ("git", "tmux", "python3"):
        path = shutil.which(name)
        result["tools"][name] = {"ok": bool(path), "path": path}

    if npm["stale_dirs"]:
        result["warnings"].append(
            "Codex npm has stale .codex-* directories; npm upgrades can fail with ENOTEMPTY. "
            "Do not delete automatically: inspect/rename the stale temp directory, run `npm cache verify`, then reinstall @openai/codex if needed."
        )
    if result["providers"]["codex"]["status"] == "BROKEN_INSTALL":
        result["warnings"].append(
            "Codex appears partially installed. Verify the active Node/NVM prefix and the codex bin link before reinstalling."
        )
    return result


def render(data: dict[str, Any]) -> str:
    lines = ["OrchBridge provider doctor", "--------------------------"]
    for key in ("codex", "claude", "commandcode", "agy"):
        row = data["providers"][key]
        detail = row.get("version") or row.get("reason") or ""
        lines.append(f"{row['label']:<14} {row['status']:<26} {detail}")
        if row.get("path"):
            lines.append(f"  path: {row['path']}")
    lines += ["", "Core tools"]
    for name, row in data["tools"].items():
        lines.append(f"{name:<14} {'HEALTHY' if row.get('ok') else 'MISSING':<26} {row.get('path') or ''}")
    npm = data.get("codex_npm") or {}
    if npm.get("observations") or npm.get("stale_dirs"):
        lines += ["", "Codex npm"]
        for obs in npm.get("observations") or []:
            if obs.get("package_path"):
                lines.append(f"  package: {obs['package_path']}")
            if obs.get("bin_path"):
                lines.append(f"  bin: {obs['bin_path']}")
        for stale in npm.get("stale_dirs") or []:
            lines.append(f"  stale: {stale}")
    if data.get("warnings"):
        lines += ["", "Warnings"]
        lines.extend(f"- {warning}" for warning in data["warnings"])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="OrchBridge provider/runtime doctor")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    data = collect()
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
