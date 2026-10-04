#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

from provider_runtime import effective_provider_enabled, resolve_provider_cli

HOME = Path.home()
CONFIG_DIR = HOME / ".config/orchbridge"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULT_MODELS = {
    "commandcode": "xiaomi/mimo-v2.5-pro",
    "gemini_low": "gemini-3.8-flash-low",
    "gemini_medium": "gemini-3.8-flash-medium",
    "gemini_high": "gemini-3.8-flash-high",
    "claude_sonnet": "claude-sonnet-5-5",
    "claude_opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-4-6-thinking",
    "opus": "claude-opus-4-6-thinking",
}
PROVIDER_BINARIES = {
    "codex": "codex",
    "claude": "claude",
    "agy": "agy",
    "commandcode": "cmd",
}


def defaults() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "default_repo": str(Path.cwd().resolve()),
        "providers": {
            name: {"enabled": "auto", "binary": binary}
            for name, binary in PROVIDER_BINARIES.items()
        },
        "models": dict(DEFAULT_MODELS),
        "routing": {
            "max_continuations": 2,
            "gemini_reserve": 0.10,
            "third_party_reserve": 0.10,
            "reset_soon_seconds": 3600,
        },
        "ui": {"notify_mode": "smart"},
    }


LEGACY_CLAUDE_MODEL_ALIASES = {
    "claude_sonnet": {"sonnet", "claude-sonnet-5", "claude-sonnet-5-0"},
    "claude_opus": {"opus", "claude-opus-5", "claude-opus-5-0"},
}


def migrate_legacy_claude_models(data: dict[str, Any]) -> list[str]:
    """Upgrade only old OrchBridge defaults; preserve explicit custom model IDs."""
    models = data.setdefault("models", {})
    if not isinstance(models, dict):
        models = {}
        data["models"] = models
    changed: list[str] = []
    for key, aliases in LEGACY_CLAUDE_MODEL_ALIASES.items():
        value = str(models.get(key) or "").strip().lower()
        if value in aliases:
            models[key] = DEFAULT_MODELS[key]
            changed.append(key)
        elif key not in models:
            models[key] = DEFAULT_MODELS[key]
    return changed


def load() -> dict[str, Any]:
    try:
        obj = json.loads(CONFIG_PATH.read_text())
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return defaults()


def save(data: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, CONFIG_PATH)


def detect() -> dict[str, Any]:
    data = load()
    rows: dict[str, Any] = {}
    for name, binary in PROVIDER_BINARIES.items():
        item = provider_state(data, name)
        configured_binary = str(item.get("binary") or binary)
        path = resolve_provider_cli(name, data, use_cache=False)
        rows[name] = {
            "binary": configured_binary,
            "path": path,
            "installed": bool(path),
        }
    return rows


def ensure_config(*, overwrite: bool = False) -> dict[str, Any]:
    if CONFIG_PATH.exists() and not overwrite:
        data = load()
        changed = migrate_legacy_claude_models(data)
        if changed:
            save(data)
        return data
    data = defaults()
    save(data)
    return data


def provider_state(data: dict[str, Any], name: str) -> dict[str, Any]:
    providers = data.setdefault("providers", {})
    item = providers.setdefault(name, {"enabled": "auto", "binary": PROVIDER_BINARIES[name]})
    if not isinstance(item, dict):
        item = {"enabled": item, "binary": PROVIDER_BINARIES[name]}
        providers[name] = item
    return item


def effective_enabled(data: dict[str, Any], name: str) -> bool:
    return effective_provider_enabled(data, name)


def show(data: dict[str, Any], as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(f"config: {CONFIG_PATH}")
    print(f"default repo: {data.get('default_repo')}")
    print("providers:")
    for name in PROVIDER_BINARIES:
        item = provider_state(data, name)
        binary = str(item.get("binary") or PROVIDER_BINARIES[name])
        path = resolve_provider_cli(name, data, use_cache=False)
        print(
            f"  {name:<12} configured={item.get('enabled','auto')!s:<5} "
            f"detected={'yes' if path else 'no ':<3} "
            f"effective={'on' if effective_enabled(data,name) else 'off'} "
            f"binary={binary}"
        )
    print("models:")
    for key, value in sorted((data.get("models") or {}).items()):
        print(f"  {key:<18} {value}")


def main() -> int:
    ap = argparse.ArgumentParser(prog="orch settings", description="OrchBridge provider/model settings")
    sub = ap.add_subparsers(dest="cmd")
    init = sub.add_parser("init", help="create a portable default config")
    init.add_argument("--force", action="store_true")
    init.add_argument("--json", action="store_true")
    sh = sub.add_parser("show", help="show current settings")
    sh.add_argument("--json", action="store_true")
    det = sub.add_parser("detect", help="detect installed provider CLIs")
    det.add_argument("--json", action="store_true")
    prov = sub.add_parser("provider", help="enable/disable provider")
    prov.add_argument("name", choices=sorted(PROVIDER_BINARIES))
    prov.add_argument("state", choices=["auto", "on", "off"])
    model = sub.add_parser("model", help="set a model alias/id")
    model.add_argument("key", choices=sorted(DEFAULT_MODELS))
    model.add_argument("value")
    repo = sub.add_parser("repo", help="set default repository")
    repo.add_argument("path")
    reset = sub.add_parser("reset", help="reset config to defaults")
    reset.add_argument("--yes", action="store_true")
    ns = ap.parse_args()

    if ns.cmd == "init":
        data = ensure_config(overwrite=ns.force)
        show(data, ns.json)
        return 0
    data = ensure_config()
    if ns.cmd in {None, "show"}:
        show(data, getattr(ns, "json", False))
        return 0
    if ns.cmd == "detect":
        found = detect()
        if ns.json:
            print(json.dumps(found, ensure_ascii=False, indent=2))
        else:
            for k,v in found.items():
                print(f"{k:<12} {'FOUND' if v['installed'] else 'missing':<7} {v['path'] or v['binary']}")
        return 0
    if ns.cmd == "provider":
        provider_state(data, ns.name)["enabled"] = {"auto":"auto","on":True,"off":False}[ns.state]
        save(data)
        print(f"{ns.name}: {ns.state}")
        return 0
    if ns.cmd == "model":
        data.setdefault("models", {})[ns.key] = ns.value
        save(data)
        print(f"{ns.key}: {ns.value}")
        return 0
    if ns.cmd == "repo":
        data["default_repo"] = str(Path(ns.path).expanduser().resolve())
        save(data)
        print(data["default_repo"])
        return 0
    if ns.cmd == "reset":
        if not ns.yes:
            raise SystemExit("refusing reset without --yes")
        data = defaults(); save(data); show(data)
        return 0
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
