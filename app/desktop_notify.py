#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
from typing import Any


def _clip(value: str, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def backend_status() -> str:
    if os.environ.get("AI_ORCH_NOTIFY_DRY_RUN") == "1":
        return "dry-run"
    sysname = platform.system()
    if sysname == "Darwin":
        if shutil.which("terminal-notifier"):
            return "terminal-notifier"
        if shutil.which("osascript"):
            return "osascript"
        return "unavailable (terminal-notifier/osascript missing)"
    if sysname == "Linux":
        if shutil.which("notify-send"):
            return "notify-send"
        return "unavailable (install libnotify/notify-send)"
    return f"unavailable ({sysname or 'unknown'} unsupported)"


def send_notification(
    *,
    title: str,
    subtitle: str = "",
    message: str,
    sound: str = "Glass",
    group: str = "orchbridge",
    timeout: float = 8.0,
) -> dict[str, Any]:
    title = _clip(title, 128)
    subtitle = _clip(subtitle, 180)
    message = _clip(message, 500)
    sound = _clip(sound, 64)
    group = _clip(group, 180)

    if os.environ.get("AI_ORCH_NOTIFY_DRY_RUN") == "1":
        return {
            "ok": True,
            "backend": "dry-run",
            "title": title,
            "subtitle": subtitle,
            "message": message,
            "sound": sound,
            "group": group,
        }

    sysname = platform.system()
    if sysname == "Linux":
        notify_send = shutil.which("notify-send")
        if not notify_send:
            return {"ok": False, "backend": "none", "reason": "notify-send is unavailable; install libnotify-bin/libnotify"}
        body = message if not subtitle else f"{subtitle}\n{message}"
        argv = [notify_send, "--app-name", "OrchBridge", title, body]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            if p.returncode == 0:
                return {"ok": True, "backend": "notify-send"}
            return {
                "ok": False,
                "backend": "notify-send",
                "reason": (p.stderr or p.stdout or f"exit {p.returncode}").strip()[-1000:],
            }
        except Exception as e:
            return {"ok": False, "backend": "notify-send", "reason": repr(e)}

    if sysname != "Darwin":
        return {"ok": False, "backend": "none", "reason": f"unsupported platform: {sysname}"}

    tn = shutil.which("terminal-notifier")
    if tn:
        argv = [tn, "-title", title, "-message", message, "-group", group]
        if subtitle:
            argv += ["-subtitle", subtitle]
        if sound:
            argv += ["-sound", sound]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            if p.returncode == 0:
                return {"ok": True, "backend": "terminal-notifier"}
            return {
                "ok": False,
                "backend": "terminal-notifier",
                "reason": (p.stderr or p.stdout or f"exit {p.returncode}").strip()[-1000:],
            }
        except Exception as e:
            return {"ok": False, "backend": "terminal-notifier", "reason": repr(e)}

    osa = shutil.which("osascript")
    if not osa:
        return {"ok": False, "backend": "none", "reason": "osascript is unavailable"}

    if sound:
        script = """on run argv
set theTitle to item 1 of argv
set theSubtitle to item 2 of argv
set theMessage to item 3 of argv
set theSound to item 4 of argv
display notification theMessage with title theTitle subtitle theSubtitle sound name theSound
end run
"""
        args = [title, subtitle, message, sound]
    else:
        script = """on run argv
set theTitle to item 1 of argv
set theSubtitle to item 2 of argv
set theMessage to item 3 of argv
display notification theMessage with title theTitle subtitle theSubtitle
end run
"""
        args = [title, subtitle, message]
    try:
        p = subprocess.run([osa, "-e", script, *args], capture_output=True, text=True, timeout=timeout)
        if p.returncode == 0:
            return {"ok": True, "backend": "osascript"}
        return {
            "ok": False,
            "backend": "osascript",
            "reason": (p.stderr or p.stdout or f"exit {p.returncode}").strip()[-1000:],
        }
    except Exception as e:
        return {"ok": False, "backend": "osascript", "reason": repr(e)}


def main() -> int:
    ap = argparse.ArgumentParser(description="OrchBridge desktop notification helper (macOS/Linux)")
    sub = ap.add_subparsers(dest="command")
    test = sub.add_parser("test", help="send one test notification")
    test.add_argument("--project", default="current-project")
    test.add_argument("--json", action="store_true")
    send = sub.add_parser("send", help="send a custom notification")
    send.add_argument("--title", required=True)
    send.add_argument("--subtitle", default="")
    send.add_argument("--message", required=True)
    send.add_argument("--sound", default="Glass")
    send.add_argument("--group", default="orchbridge")
    send.add_argument("--json", action="store_true")
    ns = ap.parse_args()

    if ns.command in {None, "test"}:
        project = getattr(ns, "project", "current-project")
        result = send_notification(
            title=f"OrchBridge · {project}",
            subtitle="알림 테스트",
            message="데스크탑 알림이 정상적으로 연결되었습니다.",
            sound="Glass",
            group=f"orchbridge:{project}:test",
        )
    else:
        result = send_notification(
            title=ns.title,
            subtitle=ns.subtitle,
            message=ns.message,
            sound=ns.sound,
            group=ns.group,
        )

    if getattr(ns, "json", False):
        print(json.dumps(result, ensure_ascii=False))
    else:
        if result.get("ok"):
            print(f"notification sent · backend={result.get('backend')}")
        else:
            print(f"notification failed · {result.get('reason') or 'unknown'}")
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
