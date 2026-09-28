#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

HOME = Path.home()
CONFIG_DIR = HOME / ".config/orchbridge"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULT_MODELS = {
    "commandcode": "xiaomi/mimo-v2.5-pro",
    "gemini_low": "gemini-3.8-flash-low",
    "gemini_medium": "gemini-3.8-flash-medium",
    "gemini_high": "gemini-3.8-flash-high",
    "claude_sonnet": "sonnet",
    "claude_opus": "opus",
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
    return {
        name: {
            "binary": binary,
            "path": shutil.which(binary),
            "installed": bool(shutil.which(binary)),
        }
        for name, binary in PROVIDER_BINARIES.items()
    }


def ensure_config(*, overwrite: bool = False) -> dict[str, Any]:
    if CONFIG_PATH.exists() and not overwrite:
        return load()
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
    item = provider_state(data, name)
    value = item.get("enabled", "auto")
    if value is False or str(value).lower() in {"false", "off", "0", "disabled"}:
        return False
    binary = str(item.get("binary") or PROVIDER_BINARIES[name])
    if value is True or str(value).lower() in {"true", "on", "1", "enabled"}:
        return shutil.which(binary) is not None
    return shutil.which(binary) is not None


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
        print(
            f"  {name:<12} configured={item.get('enabled','auto')!s:<5} "
            f"detected={'yes' if shutil.which(binary) else 'no ':<3} "
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
