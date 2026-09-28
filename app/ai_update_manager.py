#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import platform
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

HOME = Path.home()
BASE = HOME / ".local/share/orchbridge"
UPDATES_DIR = BASE / "updates"
CONFIG_PATH = UPDATES_DIR / "config.json"
HISTORY_PATH = UPDATES_DIR / "update-history.jsonl"
LAUNCH_AGENT = HOME / "Library/LaunchAgents/io.orchbridge.provider-update.plist"
WRAPPER = HOME / ".local/bin/ai-orch-update"

UPDATES_DIR.mkdir(parents=True, exist_ok=True)

PROVIDERS = ("codex", "agy", "cmd")


def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or now()).astimezone().isoformat(timespec="seconds")


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else dict(default)
    except Exception:
        return dict(default)


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)


def append_jsonl(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def config() -> dict[str, Any]:
    return load_json(
        CONFIG_PATH,
        {
            "auto_enabled": False,
            "preferred_time": "09:00",
            "last_auto_success_date": None,
            "updated_at": None,
        },
    )


def save_config(cfg: dict[str, Any]) -> None:
    cfg["updated_at"] = iso()
    atomic_json(CONFIG_PATH, cfg)


def run(
    argv: list[str],
    *,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def binary(provider: str) -> str | None:
    names = {
        "codex": "codex",
        "agy": "agy",
        "cmd": "cmd",
    }
    return shutil.which(names[provider])


def version(provider: str) -> str:
    exe = binary(provider)
    if not exe:
        return "NOT_INSTALLED"

    try:
        p = run([exe, "--version"], timeout=15)
        text = (p.stdout or p.stderr).strip().splitlines()
        return text[0].strip() if text else f"exit={p.returncode}"
    except Exception as e:
        return f"ERROR: {e}"


def provider_check(provider: str) -> dict[str, Any]:
    installed = version(provider)
    exe = binary(provider)
    if not exe:
        return {
            "provider": provider,
            "installed": installed,
            "available": None,
            "check": "not installed",
            "rc": 127,
        }

    if provider == "cmd":
        try:
            p = run([exe, "update", "--check-only"], timeout=60)
            output = (p.stdout or p.stderr).strip()
            return {
                "provider": provider,
                "installed": installed,
                "available": output or "check completed",
                "check": "cmd update --check-only",
                "rc": p.returncode,
            }
        except Exception as e:
            return {
                "provider": provider,
                "installed": installed,
                "available": None,
                "check": f"failed: {e}",
                "rc": 1,
            }

    if provider == "codex":
        npm = shutil.which("npm")
        if not npm:
            return {
                "provider": provider,
                "installed": installed,
                "available": None,
                "check": "npm unavailable; use update now for authoritative refresh",
                "rc": 0,
            }
        try:
            p = run([npm, "view", "@openai/codex", "version"], timeout=30)
            latest = (p.stdout or p.stderr).strip().splitlines()
            return {
                "provider": provider,
                "installed": installed,
                "available": latest[-1].strip() if latest else None,
                "check": "npm view @openai/codex version",
                "rc": p.returncode,
            }
        except Exception as e:
            return {
                "provider": provider,
                "installed": installed,
                "available": None,
                "check": f"failed: {e}",
                "rc": 1,
            }

    return {
        "provider": provider,
        "installed": installed,
        "available": None,
        "check": "no documented check-only mode; `agy update` is used for installation",
        "rc": 0,
    }


def job_busy() -> tuple[bool, str]:
    patterns = (
        "ai_job_worker.py",
        "phase1_supervisor.py delegate",
    )
    pgrep = shutil.which("pgrep")
    if pgrep:
        for pattern in patterns:
            try:
                p = run([pgrep, "-f", pattern], timeout=3)
                if p.returncode == 0 and p.stdout.strip():
                    return True, f"active process matches {pattern!r}"
            except Exception:
                pass
    return False, ""


def smoke(provider: str) -> dict[str, Any]:
    exe = binary(provider)
    if not exe:
        return {"ok": False, "reason": "binary missing"}

    try:
        p = run([exe, "--help"], timeout=20)
        return {
            "ok": p.returncode == 0,
            "rc": p.returncode,
            "output_tail": ((p.stdout or "") + "\n" + (p.stderr or ""))[-1200:],
        }
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def update_provider(provider: str) -> dict[str, Any]:
    exe = binary(provider)
    before = version(provider)
    started = now()

    if not exe:
        result = {
            "ts": iso(),
            "provider": provider,
            "status": "FAILED",
            "before": before,
            "after": "NOT_INSTALLED",
            "duration_seconds": 0,
            "reason": "binary not installed",
        }
        append_jsonl(HISTORY_PATH, result)
        return result

    commands = {
        "codex": [exe, "--upgrade"],
        "agy": [exe, "update"],
        "cmd": [exe, "update"],
    }
    argv = commands[provider]

    try:
        p = run(argv, timeout=600)
        output = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
        after = version(provider)
        smoke_result = smoke(provider)
        ok = p.returncode == 0 and bool(smoke_result.get("ok"))
        result = {
            "ts": iso(),
            "provider": provider,
            "status": "COMPLETE" if ok else "FAILED",
            "command": argv,
            "before": before,
            "after": after,
            "duration_seconds": round(now() - started, 3),
            "update_rc": p.returncode,
            "smoke": smoke_result,
            "output_tail": output[-3000:],
        }
    except subprocess.TimeoutExpired:
        result = {
            "ts": iso(),
            "provider": provider,
            "status": "FAILED",
            "command": argv,
            "before": before,
            "after": version(provider),
            "duration_seconds": round(now() - started, 3),
            "reason": "update timeout",
        }
    except Exception as e:
        result = {
            "ts": iso(),
            "provider": provider,
            "status": "FAILED",
            "command": argv,
            "before": before,
            "after": version(provider),
            "duration_seconds": round(now() - started, 3),
            "reason": str(e),
        }

    append_jsonl(HISTORY_PATH, result)
    return result


def target_list(value: str) -> list[str]:
    if value == "all":
        return list(PROVIDERS)
    if value not in PROVIDERS:
        raise SystemExit("provider must be all|codex|agy|cmd")
    return [value]


def print_status() -> int:
    cfg = config()
    busy, busy_reason = job_busy()
    history = []
    if HISTORY_PATH.exists():
        for line in HISTORY_PATH.read_text().splitlines()[-20:]:
            try:
                history.append(json.loads(line))
            except Exception:
                pass

    last_by_provider: dict[str, Any] = {}
    for row in history:
        if isinstance(row, dict) and row.get("provider") in PROVIDERS:
            last_by_provider[str(row["provider"])] = row

    print("PROVIDER UPDATE MANAGER")
    print()
    for provider in PROVIDERS:
        last = last_by_provider.get(provider)
        print(f"{provider:5}  {version(provider)}")
        if last:
            print(
                f"       last {last.get('status')} · "
                f"{last.get('before')} -> {last.get('after')} · {last.get('ts')}"
            )
    print()
    print(
        f"auto   {'ON' if cfg.get('auto_enabled') else 'OFF'}"
        f" · preferred {cfg.get('preferred_time', '09:00')}"
    )
    print(f"launchagent {'installed' if LAUNCH_AGENT.exists() else 'not installed'}")
    print(f"busy   {'YES · ' + busy_reason if busy else 'NO'}")
    return 0


def check_updates(provider: str) -> int:
    rows = [provider_check(x) for x in target_list(provider)]
    for row in rows:
        print(f"{row['provider'].upper()}")
        print(f"  installed  {row.get('installed')}")
        print(f"  available  {row.get('available') or 'unknown'}")
        print(f"  check      {row.get('check')}")
        print()
    return 0 if all(int(x.get("rc") or 0) == 0 for x in rows) else 1


def update_now(provider: str, *, scheduled: bool = False) -> int:
    busy, reason = job_busy()
    if busy:
        print(f"DEFERRED: orchestrator is busy ({reason}).")
        return 10 if scheduled else 2

    rows = []
    for target in target_list(provider):
        print(f"UPDATE {target.upper()} …", flush=True)
        row = update_provider(target)
        rows.append(row)
        print(
            f"  {row.get('status')} · {row.get('before')} -> {row.get('after')} "
            f"· {row.get('duration_seconds')}s"
        )

    ok = all(row.get("status") == "COMPLETE" for row in rows)
    return 0 if ok else 1


def parse_time(value: str) -> tuple[int, int]:
    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value)
    if not m:
        raise SystemExit("time must be HH:MM")
    return int(m.group(1)), int(m.group(2))


def launchagent_text() -> str:
    path = str(WRAPPER)
    out = str(UPDATES_DIR / "scheduler.out.log")
    err = str(UPDATES_DIR / "scheduler.err.log")
    user_path = (
        f"{HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:"
        "/usr/bin:/bin:/usr/sbin:/sbin"
    )
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>io.orchbridge.provider-update</string>
  <key>ProgramArguments</key>
  <array>
    <string>{html.escape(path)}</string>
    <string>scheduled</string>
  </array>
  <key>StartInterval</key>
  <integer>3600</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>{html.escape(user_path)}</string>
    <key>HOME</key>
    <string>{html.escape(str(HOME))}</string>
  </dict>
  <key>StandardOutPath</key>
  <string>{html.escape(out)}</string>
  <key>StandardErrorPath</key>
  <string>{html.escape(err)}</string>
</dict>
</plist>
'''


def launchctl_bootstrap() -> tuple[bool, str]:
    if platform.system() != "Darwin":
        return False, "automatic scheduling requires macOS launchd"

    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT.write_text(launchagent_text())

    uid = str(os.getuid())
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", str(LAUNCH_AGENT)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    p = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(LAUNCH_AGENT)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if p.returncode != 0:
        return False, (p.stderr or p.stdout).strip()
    return True, "launchd agent loaded"


def launchctl_remove() -> tuple[bool, str]:
    if LAUNCH_AGENT.exists() and platform.system() == "Darwin":
        uid = str(os.getuid())
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}", str(LAUNCH_AGENT)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    try:
        LAUNCH_AGENT.unlink(missing_ok=True)
    except Exception as e:
        return False, str(e)
    return True, "automatic update agent removed"


def auto_on(when: str) -> int:
    parse_time(when)
    cfg = config()
    cfg["auto_enabled"] = True
    cfg["preferred_time"] = when
    save_config(cfg)

    ok, message = launchctl_bootstrap()
    print(f"auto update ON · preferred {when}")
    print(message)
    return 0 if ok else 1


def auto_off() -> int:
    cfg = config()
    cfg["auto_enabled"] = False
    save_config(cfg)
    ok, message = launchctl_remove()
    print("auto update OFF")
    print(message)
    return 0 if ok else 1


def due_today(cfg: dict[str, Any]) -> tuple[bool, str]:
    if not cfg.get("auto_enabled"):
        return False, "auto update disabled"

    today = datetime.now().astimezone().date().isoformat()
    if cfg.get("last_auto_success_date") == today:
        return False, "already updated today"

    hour, minute = parse_time(str(cfg.get("preferred_time", "09:00")))
    local = datetime.now().astimezone()
    if (local.hour, local.minute) < (hour, minute):
        return False, f"before preferred time {hour:02d}:{minute:02d}"

    return True, "due"


def scheduled() -> int:
    cfg = config()
    due, reason = due_today(cfg)
    if not due:
        print(f"SKIP: {reason}")
        return 0

    rc = update_now("all", scheduled=True)
    if rc == 10:
        return 0

    if rc == 0:
        cfg = config()
        cfg["last_auto_success_date"] = datetime.now().astimezone().date().isoformat()
        save_config(cfg)
    return rc


def show_log(limit: int) -> int:
    if not HISTORY_PATH.exists():
        print("No update history.")
        return 0

    rows = []
    for line in HISTORY_PATH.read_text().splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except Exception:
            pass

    if not rows:
        print("No update history.")
        return 0

    for row in rows:
        print(
            f"{row.get('ts')} · {str(row.get('provider')).upper()} · "
            f"{row.get('status')} · {row.get('before')} -> {row.get('after')}"
        )
        if row.get("reason"):
            print(f"  reason: {row.get('reason')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="OrchBridge provider CLI update manager")
    sub = p.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status")
    status.set_defaults(fn=lambda _a: print_status())

    check = sub.add_parser("check")
    check.add_argument("provider", nargs="?", default="all", choices=["all", *PROVIDERS])
    check.set_defaults(fn=lambda a: check_updates(a.provider))

    nowp = sub.add_parser("now")
    nowp.add_argument("provider", nargs="?", default="all", choices=["all", *PROVIDERS])
    nowp.set_defaults(fn=lambda a: update_now(a.provider))

    auto = sub.add_parser("auto")
    auto_sub = auto.add_subparsers(dest="auto_command", required=True)
    on = auto_sub.add_parser("on")
    on.add_argument("time", nargs="?", default="09:00")
    on.set_defaults(fn=lambda a: auto_on(a.time))
    off = auto_sub.add_parser("off")
    off.set_defaults(fn=lambda _a: auto_off())

    log = sub.add_parser("log")
    log.add_argument("--limit", type=int, default=20)
    log.set_defaults(fn=lambda a: show_log(a.limit))

    sch = sub.add_parser("scheduled")
    sch.set_defaults(fn=lambda _a: scheduled())

    return p


def main() -> int:
    args = build_parser().parse_args()
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
