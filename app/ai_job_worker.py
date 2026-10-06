#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

from orch_runtime import make_main_lease, process_pgid, refresh_main_lease, write_main_lease


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location("ai_chat_legacy_runtime", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--task-file", required=True)
    ap.add_argument("--raw-file", required=True)
    ap.add_argument("--state-file", required=True)
    ap.add_argument("--result-file", required=True)
    ap.add_argument("--lease-file")
    ap.add_argument("--job-id", default=os.getenv("AI_ORCH_JOB_ID", "legacy-job"))
    ap.add_argument("--run-number", type=int, default=1)
    ap.add_argument("--prompt-sha", default=os.getenv("AI_ORCH_PROMPT_SHA256", ""))
    ns = ap.parse_args()

    app_dir = Path(__file__).resolve().parent
    installed_runtime = Path.home() / ".local/share/orchbridge/app/ai_chat.py"
    legacy_path = installed_runtime if installed_runtime.is_file() else (app_dir / "ai_chat.py")
    legacy = load_module(legacy_path)
    repo = Path(ns.repo).expanduser().resolve()
    task = Path(ns.task_file).read_text()
    raw = Path(ns.raw_file).read_text()
    state = json.loads(Path(ns.state_file).read_text())
    out = Path(ns.result_file).expanduser().resolve()
    lease_path = Path(ns.lease_file).expanduser().resolve() if ns.lease_file else None

    stop = threading.Event()
    hb: threading.Thread | None = None
    if lease_path:
        write_main_lease(
            lease_path,
            job_id=ns.job_id,
            repo=str(repo),
            pid=os.getpid(),
            pgid=process_pgid(os.getpid()) or os.getpid(),
            run_number=ns.run_number,
            prompt_sha256=ns.prompt_sha,
            result_file=str(out),
            state="RUNNING",
        )

        def heartbeat() -> None:
            while not stop.wait(10):
                try:
                    refresh_main_lease(lease_path, state="RUNNING")
                except Exception:
                    pass

        hb = threading.Thread(
            target=heartbeat,
            name=f"main-lease-{ns.job_id}",
            daemon=True,
        )
        hb.start()

    rc = 1
    try:
        result = legacy.run_ai_orch(repo, task, raw, state)
        rc = int(result[0]) if len(result) > 0 else 1
        payload = {
            "rc": rc,
            "response": str(result[1]) if len(result) > 1 else "",
            "stderr_text": str(result[2]) if len(result) > 2 else "",
            "main_name": result[3] if len(result) > 3 else None,
            "task_status": result[4] if len(result) > 4 else None,
        }
        _atomic_json(out, payload)
        return rc
    except KeyboardInterrupt:
        rc = 130
        _atomic_json(out, {"rc": rc, "response": "", "task_status": None})
        return rc
    except Exception as exc:
        traceback.print_exc()
        rc = 1
        _atomic_json(
            out,
            {
                "rc": rc,
                "response": "",
                "stderr_text": repr(exc),
                "main_name": None,
                "task_status": None,
            },
        )
        return rc
    finally:
        stop.set()
        if hb and hb.is_alive():
            hb.join(timeout=1)
        if lease_path:
            try:
                data = make_main_lease(
                    job_id=ns.job_id,
                    repo=str(repo),
                    pid=os.getpid(),
                    pgid=process_pgid(os.getpid()) or os.getpid(),
                    run_number=ns.run_number,
                    prompt_sha256=ns.prompt_sha,
                    result_file=str(out),
                    state="EXITED",
                )
                data["exit_code"] = int(rc)
                data["ended_at_epoch"] = time.time()
                _atomic_json(lease_path, data)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
