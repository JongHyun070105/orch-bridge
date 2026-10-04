from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def _alias(model: str | None) -> str:
    m = str(model or "").lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    return str(model or "").strip()


def pretty_model(model: str | None) -> str:
    raw = str(model or "").strip()
    lo = raw.lower()
    m = re.search(r"claude-(opus|sonnet)-(\d+)-(\d+)", lo)
    if m:
        family, major, minor = m.groups()
        return f"Claude {family.title()} {major}.{minor}"
    if lo == "opus":
        return "Claude Opus"
    if lo == "sonnet":
        return "Claude Sonnet"
    return raw or "Claude"


def configured_effort(
    model: str | None,
    *,
    home: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str | None, str]:
    env = os.environ if env is None else env
    direct = str(env.get("CLAUDE_CODE_EFFORT_LEVEL") or "").strip().lower()
    if direct in VALID_EFFORTS:
        return direct, "env"

    home = Path.home() if home is None else home
    settings = home / ".claude" / "settings.json"
    try:
        data = json.loads(settings.read_text())
    except Exception:
        return None, "default"
    if not isinstance(data, dict):
        return None, "default"

    model_settings = data.get("modelSettings")
    if isinstance(model_settings, dict):
        for key in [str(model or "").strip(), _alias(model)]:
            item = model_settings.get(key)
            if isinstance(item, dict):
                value = str(item.get("effortLevel") or "").strip().lower()
                if value in VALID_EFFORTS:
                    return value, f"modelSettings:{key}"

    value = str(data.get("effortLevel") or "").strip().lower()
    if value in VALID_EFFORTS:
        return value, "settings"
    return None, "default"


def adaptive_effort(
    *,
    need: float,
    reasoning: float,
    uncertainty: float,
    scope: float,
    risk: float,
    crosscheck: float,
    env: dict[str, str] | None = None,
) -> tuple[str, str]:
    env = os.environ if env is None else env
    direct = str(
        env.get("AI_ORCH_CLAUDE_EFFORT_LEVEL")
        or env.get("CLAUDE_CODE_EFFORT_LEVEL")
        or ""
    ).strip().lower()
    if direct in VALID_EFFORTS:
        return direct, "env"

    vals = [need, reasoning, uncertainty, scope, risk, crosscheck]
    n, r, u, sc, rk, cc = [max(0.0, min(1.0, float(v))) for v in vals]
    pressure = max(
        n,
        0.70 * r + 0.30 * u,
        0.78 * rk + 0.22 * u,
        0.72 * cc + 0.28 * sc,
    )
    if pressure < 0.34:
        return "low", "adaptive"
    if pressure < 0.60:
        return "medium", "adaptive"
    if pressure < 0.82:
        return "high", "adaptive"
    return "xhigh", "adaptive"


def effective_effort(
    model: str | None,
    *,
    need: float,
    reasoning: float,
    uncertainty: float,
    scope: float,
    risk: float,
    crosscheck: float,
    home: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str | None, str]:
    env = os.environ if env is None else env
    mode = str(env.get("AI_ORCH_CLAUDE_EFFORT_MODE") or "auto").strip().lower()
    if mode in {"settings", "manual", "claude-settings"}:
        return configured_effort(model, home=home, env=env)
    return adaptive_effort(
        need=need,
        reasoning=reasoning,
        uncertainty=uncertainty,
        scope=scope,
        risk=risk,
        crosscheck=crosscheck,
        env=env,
    )


def cli_supports_effort(exe: str) -> bool:
    try:
        p = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=4)
        return "--effort" in (p.stdout + "\n" + p.stderr)
    except Exception:
        return False


def stream_actions(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract user-visible Claude Code activity without exposing hidden CoT."""
    if not isinstance(obj, dict):
        return []
    out: list[dict[str, Any]] = []
    etype = obj.get("type")
    if etype == "system" and obj.get("subtype") == "init":
        out.append({"kind": "init", "model": obj.get("model")})
        return out

    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    content = message.get("content") if isinstance(message.get("content"), list) else []
    if etype == "assistant":
        reasoning_seen = False
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if btype in {"thinking", "redacted_thinking"}:
                if not reasoning_seen:
                    out.append({"kind": "reasoning"})
                    reasoning_seen = True
            elif btype == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    out.append({"kind": "text", "text": text})
            elif btype == "tool_use":
                out.append({
                    "kind": "tool",
                    "name": str(block.get("name") or "tool"),
                    "input": block.get("input"),
                    "id": block.get("id"),
                })
    elif etype == "user":
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            out.append({
                "kind": "tool_result",
                "id": block.get("tool_use_id"),
                "is_error": bool(block.get("is_error")),
            })
    return out
