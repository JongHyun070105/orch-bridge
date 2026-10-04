#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

HOME = Path.home()
CACHE = HOME / ".cache/orchbridge"
GLOBAL_HEALTH_PATH = CACHE / "provider-global-health.json"
LOCK_PATH = CACHE / "provider-global-health.lock"


def _load_unlocked() -> dict[str, Any]:
    try:
        data = json.loads(GLOBAL_HEALTH_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_unlocked(data: dict[str, Any]) -> None:
    GLOBAL_HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = GLOBAL_HEALTH_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, GLOBAL_HEALTH_PATH)


def _mutate(fn: Callable[[dict[str, Any]], Any]) -> Any:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        data = _load_unlocked()
        data.setdefault("schema", 1)
        providers = data.setdefault("providers", {})
        if not isinstance(providers, dict):
            providers = {}
            data["providers"] = providers
        result = fn(data)
        _write_unlocked(data)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return result


def all_status() -> dict[str, Any]:
    data = _load_unlocked()
    providers = data.get("providers", {}) if isinstance(data, dict) else {}
    return providers if isinstance(providers, dict) else {}


def provider_status(provider: str) -> dict[str, Any]:
    item = all_status().get(provider)
    return dict(item) if isinstance(item, dict) else {}


def provider_unavailable(provider: str) -> bool:
    return str(provider_status(provider).get("status") or "").upper() in {
        "QUOTA_EXHAUSTED", "AUTH_REQUIRED"
    }


def probe_due(provider: str) -> bool:
    item = provider_status(provider)
    if str(item.get("status") or "").upper() != "QUOTA_EXHAUSTED":
        return False
    try:
        return float(item.get("next_probe_at") or 0) <= time.time()
    except Exception:
        return True


def mark_quota_exhausted(
    provider: str,
    *,
    reason: str,
    source: str,
    detail: str = "",
    retry_after_seconds: int = 3600,
) -> dict[str, Any]:
    now = time.time()

    def change(data: dict[str, Any]) -> dict[str, Any]:
        providers = data["providers"]
        old = providers.get(provider, {}) if isinstance(providers.get(provider), dict) else {}
        item = {
            "status": "QUOTA_EXHAUSTED",
            "reason": str(reason or "credit/quota exhausted"),
            "source": str(source or "unknown"),
            "detail": str(detail or "")[:500],
            "detected_at": float(old.get("detected_at") or now),
            "updated_at": now,
            "next_probe_at": now + max(300, int(retry_after_seconds)),
            "failure_count": int(old.get("failure_count") or 0) + 1,
        }
        providers[provider] = item
        return dict(item)

    return _mutate(change)


def defer_probe(provider: str, seconds: int, *, reason: str | None = None) -> dict[str, Any]:
    now = time.time()

    def change(data: dict[str, Any]) -> dict[str, Any]:
        providers = data["providers"]
        old = providers.get(provider, {}) if isinstance(providers.get(provider), dict) else {}
        if not old:
            return {}
        old["next_probe_at"] = now + max(300, int(seconds))
        old["updated_at"] = now
        if reason:
            old["probe_note"] = str(reason)[:300]
        providers[provider] = old
        return dict(old)

    return _mutate(change)


def clear_provider(provider: str, *, source: str = "success") -> None:
    def change(data: dict[str, Any]) -> None:
        providers = data["providers"]
        providers.pop(provider, None)
        data["last_clear"] = {"provider": provider, "source": source, "at": time.time()}

    _mutate(change)


def clear_all() -> None:
    def change(data: dict[str, Any]) -> None:
        data["providers"] = {}
        data["last_clear"] = {"provider": "*", "source": "manual", "at": time.time()}

    _mutate(change)


def summary(provider: str) -> str:
    item = provider_status(provider)
    if not item:
        return "HEALTHY"
    status = str(item.get("status") or "UNKNOWN")
    reason = str(item.get("reason") or "")
    try:
        wait = max(0, int(float(item.get("next_probe_at") or 0) - time.time()))
    except Exception:
        wait = 0
    suffix = f" · next probe ~{wait//60}m" if wait else ""
    return f"{status}: {reason}{suffix}".strip()
